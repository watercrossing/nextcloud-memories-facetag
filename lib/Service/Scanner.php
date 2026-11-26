<?php

declare(strict_types=1);

namespace OCA\MemoriesFacetag\Service;

use OCA\MemoriesFacetag\AppInfo\Application;
use OCP\BackgroundJob\IJobList;
use OCP\DB\QueryBuilder\IQueryBuilder;
use OCP\Files\Config\IUserMountCache;
use OCP\Files\File;
use OCP\Files\Folder;
use OCP\Files\IRootFolder;
use OCP\IDBConnection;
use OCP\ITempManager;
use Psr\Log\LoggerInterface;

/**
 * Second face-detection pass over the photo library, with looser limits than Recognize's.
 *
 * Recognize detects on a 1024px preview, drops faces scoring under 0.9 or rolled past 30°, and
 * never revisits a photo that already has a face. This pass looks at a 2048px preview, keeps faces
 * from 0.8, ignores roll (unreliable on small faces), and adds only faces that don't overlap one
 * already there — for every user who can see the photo, as Recognize does.
 *
 * A new face whose nearest named person is close enough, and clearly ahead of the next, is put
 * under that person; the rest go in unassigned, for Recognize's ClusterFacesJob to place.
 * Every face it adds is recorded in facetag_manual (source 'scan' or 'scanknn'), so undo() can take
 * the pass back out. Used by `occ memories_facetag:scan` and by ScanJob.
 */
class Scanner {
	private const CLUSTER_JOB = 'OCA\Recognize\BackgroundJobs\ClusterFacesJob';
	/** Leave freshly indexed photos to Recognize's first pass */
	private const SETTLE_SECONDS = 86400;
	private const RETRY_FAILED_SECONDS = 7 * 86400;
	private const READ_TIMEOUT = 120;
	/** Reference faces per user are reloaded this often, so new names are picked up */
	private const REFS_TTL = 900;

	public const DEFAULTS = [
		'user' => null,
		'path' => null,
		'file' => null,
		'limit' => 0,
		'deadline' => PHP_INT_MAX,
		'minScore' => 0.8,
		'maxYaw' => 50.0,
		'minSize' => 0.03,
		// Measured on 600 named faces: at 0.40 / 0.10 this names 63% of faces and disagrees with
		// the existing name 1.3% of the time, some of which is Recognize's own mistakes.
		'assignDistance' => 0.40,
		'assignMargin' => 0.10,
		'dryRun' => false,
		'cluster' => true,
	];

	private array $o = self::DEFAULTS;
	/** @var array<string, array{at: int, refs: array}> */
	private array $refs = [];
	/** @var resource|null */
	private $proc = null;
	/** @var array<int, resource> */
	private array $pipes = [];
	/** @var resource|null */
	private $lock = null;
	/** @var callable(string, bool): void */
	private $log;

	public function __construct(
		private IDBConnection $db,
		private IRootFolder $rootFolder,
		private IUserMountCache $userMountCache,
		private IJobList $jobList,
		private ITempManager $tempManager,
		private FaceStore $store,
		private PreviewImage $previewImage,
		private NodeRuntime $node,
		private LoggerInterface $logger,
	) {
		$this->log = static function (string $line, bool $verbose = false): void {
		};
	}

	/**
	 * Scan photos not yet scanned.
	 *
	 * @param array $options keys of DEFAULTS
	 * @param callable(string, bool): void|null $log gets progress lines; the bool marks per-photo detail
	 * @return array{photos: int, found: int, added: int, named: int, errors: int, users: list<string>, remaining: int, locked: bool}
	 */
	public function scan(array $options, ?callable $log = null): array {
		$this->o = array_merge(self::DEFAULTS, $options);
		if ($log !== null) {
			$this->log = $log;
		}
		$stats = ['photos' => 0, 'found' => 0, 'added' => 0, 'named' => 0, 'errors' => 0, 'users' => [], 'remaining' => 0, 'locked' => false];
		if (!$this->acquireLock()) {
			($this->log)('Another scan is running', false);
			$stats['locked'] = true;
			return $stats;
		}
		try {
			$candidates = $this->candidates();
			($this->log)(sprintf('%d photos to scan%s', count($candidates), $this->o['dryRun'] ? ' (dry run)' : ''), false);
			if ($candidates) {
				$this->run($candidates, $stats);
			}
			$stats['remaining'] = max(0, count($candidates) - $stats['photos']);
		} finally {
			$this->stopNode();
			$this->releaseLock();
		}
		if (!$this->o['dryRun'] && $this->o['cluster'] && $stats['users']) {
			$this->queueClustering($stats['users']);
		}
		return $stats;
	}

	/** Queue Recognize's clustering for these users (the job list ignores duplicates). */
	public function queueClustering(array $users): void {
		foreach ($users as $uid) {
			$this->jobList->add(self::CLUSTER_JOB, ['userId' => (string)$uid]);
		}
	}

	private function run(array $candidates, array &$stats): void {
		$this->startNode();
		$users = [];
		$started = time();
		foreach ($candidates as $fileId) {
			if (($this->o['limit'] > 0 && $stats['photos'] >= $this->o['limit']) || time() >= $this->o['deadline']) {
				($this->log)('Stopping: limit reached', false);
				break;
			}
			$stats['photos']++;
			try {
				[$found, $added, $named, $touched] = $this->scanFile($fileId);
				$stats['found'] += $found;
				$stats['added'] += $added;
				$stats['named'] += $named;
				foreach ($touched as $u) {
					$users[$u] = true;
				}
			} catch (\Throwable $e) {
				$stats['errors']++;
				$this->logger->info('facetag scan: file ' . $fileId . ': ' . $e->getMessage(), ['app' => Application::APP_ID]);
				($this->log)("$fileId: " . $e->getMessage(), true);
				if (!$this->o['dryRun']) {
					$this->markScanned($fileId, -1, 0);
				}
				if ($this->proc === null || !proc_get_status($this->proc)['running']) {
					$this->startNode();
				}
			}
			if ($stats['photos'] % 100 === 0) {
				($this->log)(sprintf('%d/%d photos, %d new faces, %d rows added, %d named, %d errors, %.2f photos/s, %d MB',
					$stats['photos'], count($candidates), $stats['found'], $stats['added'], $stats['named'], $stats['errors'],
					$stats['photos'] / max(1, time() - $started), memory_get_usage(true) >> 20), false);
				$this->userMountCache->clear();
			}
		}
		$stats['users'] = array_map('strval', array_keys($users));
	}

	/** Photos still to scan, oldest file first. @return list<int> */
	public function candidates(): array {
		if ($this->o['file'] !== null) {
			return [(int)$this->o['file']];
		}
		$qb = $this->db->getQueryBuilder();
		$qb->select('m.fileid')
			->from('memories', 'm')
			->where($qb->expr()->eq('m.isvideo', $qb->createNamedParameter(false, IQueryBuilder::PARAM_BOOL)))
			->andWhere($qb->expr()->eq('m.orphan', $qb->createNamedParameter(false, IQueryBuilder::PARAM_BOOL)))
			->andWhere($qb->expr()->lt('m.indexed_at', $qb->createNamedParameter(time() - self::SETTLE_SECONDS, IQueryBuilder::PARAM_INT)))
			// Done, or failed (faces_found -1) within the last week; older failures get another go
			->andWhere($qb->expr()->notIn('m.fileid', $qb->createFunction('(SELECT file_id FROM *PREFIX*facetag_scanned WHERE faces_found >= 0 OR scanned_at > ' . (time() - self::RETRY_FAILED_SECONDS) . ')')))
			->andWhere($qb->expr()->notIn('m.fileid', $qb->createFunction('(SELECT file_id FROM *PREFIX*recognize_queue_faces)')))
			->orderBy('m.fileid');

		$user = $this->o['user'];
		if ($this->o['path'] !== null) {
			if ($user === null) {
				throw new \InvalidArgumentException('--path needs --user');
			}
			$folder = $this->rootFolder->getUserFolder($user)->get($this->o['path']);
			if (!$folder instanceof Folder) {
				throw new \InvalidArgumentException($this->o['path'] . ' is not a folder');
			}
			$qb->innerJoin('m', 'filecache', 'f', $qb->expr()->eq('f.fileid', 'm.fileid'))
				->andWhere($qb->expr()->eq('f.storage', $qb->createNamedParameter($folder->getStorage()->getCache()->getNumericStorageId(), IQueryBuilder::PARAM_INT)))
				->andWhere($qb->expr()->like('f.path', $qb->createNamedParameter($this->db->escapeLikeParameter($folder->getInternalPath()) . '/%')));
		}

		$ids = array_map('intval', $qb->executeQuery()->fetchAll(\PDO::FETCH_COLUMN));
		if ($user !== null && $this->o['path'] === null) {
			$userFolder = $this->rootFolder->getUserFolder($user);
			$ids = array_values(array_filter($ids, fn (int $id) => $userFolder->getFirstNodeById($id) !== null));
		}
		return $ids;
	}

	/** @return array{0: int, 1: int, 2: int, 3: list<string>} new faces, rows added, rows named, users they were added for */
	private function scanFile(int $fileId): array {
		$node = $this->rootFolder->getFirstNodeById($fileId);
		if (!$node instanceof File) {
			throw new \RuntimeException('not found');
		}
		$users = array_values(array_unique(array_map(
			static fn ($mount) => $mount->getUser()->getUID(),
			$this->userMountCache->getMountsForFileId($fileId),
		)));

		$image = $this->previewImage->write($node);
		try {
			$faces = $this->detect($image);
		} finally {
			@unlink($image);
		}

		// Keep faces Recognize would cluster, minus the roll check
		$o = $this->o;
		$faces = array_values(array_filter($faces, static fn (array $f) => $f['score'] >= $o['minScore']
			&& abs($f['yaw']) <= $o['maxYaw']
			&& $f['width'] >= $o['minSize'] && $f['height'] >= $o['minSize']));
		// Best first, so overlapping duplicates of one face keep the strongest
		usort($faces, static fn (array $a, array $b) => $b['score'] <=> $a['score']);

		$taken = $this->store->boxesForFile($fileId);
		$new = [];
		foreach ($faces as $f) {
			foreach ($taken as $t) {
				if (self::overlaps($f, $t)) {
					continue 2;
				}
			}
			$new[] = $f;
			$taken[] = $f;
		}

		$rows = 0;
		$named = [];
		foreach ($new as $f) {
			foreach ($users as $uid) {
				$who = $this->confidentMatch($uid, $f['vector']);
				if ($who !== null) {
					$named[] = $who['name'];
				}
				if (!$this->o['dryRun']) {
					$this->store->insertScanned($uid, $fileId, $f, $f['vector'], $who['id'] ?? null);
				}
				$rows++;
			}
		}
		if (!$this->o['dryRun']) {
			$this->markScanned($fileId, count($new), $rows);
		}
		if ($new) {
			($this->log)(sprintf('%d %s: +%d (%s) for %s%s', $fileId, $node->getName(), count($new),
				implode(' ', array_map(static fn ($f) => sprintf('%.2f', $f['score']), $new)), implode(',', $users),
				$named ? '; named ' . implode(', ', $named) : ''), true);
		}
		return [count($new), $rows, count($named), $new ? $users : []];
	}

	/**
	 * The named person a face clearly belongs to, or null.
	 *
	 * @param list<float> $vector
	 * @return array{id: int, name: string, distance: float}|null
	 */
	private function confidentMatch(string $uid, array $vector): ?array {
		if ($this->o['assignDistance'] <= 0) {
			return null;
		}
		if (!isset($this->refs[$uid]) || $this->refs[$uid]['at'] < time() - self::REFS_TTL) {
			$this->refs[$uid] = ['at' => time(), 'refs' => $this->store->referenceFaces($uid)];
		}
		$ranked = FaceStore::rank($vector, $this->refs[$uid]['refs'], 2);
		if (!$ranked || $ranked[0]['distance'] > $this->o['assignDistance']) {
			return null;
		}
		$runnerUp = $ranked[1]['distance'] ?? INF;
		return $runnerUp - $ranked[0]['distance'] >= $this->o['assignMargin'] ? $ranked[0] : null;
	}

	/** Two boxes are the same face if either one's centre lies inside the other. */
	private static function overlaps(array $a, array $b): bool {
		$inside = static fn (array $p, array $box) => $p['x'] + $p['width'] / 2 > $box['x'] && $p['x'] + $p['width'] / 2 < $box['x'] + $box['width']
			&& $p['y'] + $p['height'] / 2 > $box['y'] && $p['y'] + $p['height'] / 2 < $box['y'] + $box['height'];
		return $inside($a, $b) || $inside($b, $a);
	}

	private function markScanned(int $fileId, int $found, int $added): void {
		$qb = $this->db->getQueryBuilder();
		$qb->delete('facetag_scanned')
			->where($qb->expr()->eq('file_id', $qb->createNamedParameter($fileId, IQueryBuilder::PARAM_INT)))
			->executeStatement();
		$qb = $this->db->getQueryBuilder();
		$qb->insert('facetag_scanned')
			->values([
				'file_id' => $qb->createNamedParameter($fileId, IQueryBuilder::PARAM_INT),
				'faces_found' => $qb->createNamedParameter($found, IQueryBuilder::PARAM_INT),
				'faces_added' => $qb->createNamedParameter($added, IQueryBuilder::PARAM_INT),
				'scanned_at' => $qb->createNamedParameter(time(), IQueryBuilder::PARAM_INT),
			])
			->executeStatement();
	}

	/**
	 * Take the scan back out: every face it named itself, and every face it added that nobody
	 * (Recognize or a person) has since put under a named person. Clears the progress table.
	 */
	public function undo(): string {
		if (!$this->acquireLock()) {
			return 'A scan is running; try again when it has finished.';
		}
		try {
			$ids = $this->db->executeQuery('SELECT m.detection_id FROM *PREFIX*facetag_manual m
				LEFT JOIN *PREFIX*recognize_face_detections d ON d.id = m.detection_id
				LEFT JOIN *PREFIX*recognize_face_clusters c ON c.id = d.cluster_id
				WHERE m.source = \'scanknn\'
					OR (m.source = \'scan\' AND (c.id IS NULL OR c.title = \'\'))')->fetchAll(\PDO::FETCH_COLUMN);
			$deleted = 0;
			foreach (array_chunk(array_map('intval', $ids), 500) as $chunk) {
				foreach (['recognize_face_detections' => 'id', 'facetag_manual' => 'detection_id'] as $table => $col) {
					$qb = $this->db->getQueryBuilder();
					$n = $qb->delete($table)
						->where($qb->expr()->in($col, $qb->createNamedParameter($chunk, IQueryBuilder::PARAM_INT_ARRAY)))
						->executeStatement();
					if ($table === 'recognize_face_detections') {
						$deleted += $n;
					}
				}
			}
			$this->db->executeStatement('DELETE FROM *PREFIX*facetag_scanned');
			$kept = (int)$this->db->executeQuery('SELECT COUNT(*) FROM *PREFIX*facetag_manual WHERE source = \'scan\'')->fetchOne();
			return "Deleted $deleted scanned faces; kept $kept that are now under a named person; scan progress cleared.";
		} finally {
			$this->releaseLock();
		}
	}

	// ------------------------------------------------------------------ lock

	/** One scan at a time, across `occ` runs and the background job (same container, same user). */
	private function acquireLock(): bool {
		$fp = fopen(rtrim($this->tempManager->getTempBaseDir(), '/') . '/' . Application::APP_ID . '-scan.lock', 'c');
		if ($fp === false || !flock($fp, LOCK_EX | LOCK_NB)) {
			if ($fp !== false) {
				fclose($fp);
			}
			return false;
		}
		$this->lock = $fp;
		return true;
	}

	private function releaseLock(): void {
		if ($this->lock !== null) {
			flock($this->lock, LOCK_UN);
			fclose($this->lock);
			$this->lock = null;
		}
	}

	// ------------------------------------------------------------ node worker

	private function startNode(): void {
		$this->stopNode();
		$env = $this->node->env(['FACETAG_MIN_SCORE' => (string)$this->o['minScore'], 'PATH' => (string)getenv('PATH')]);
		$this->proc = proc_open($this->node->niced([$this->node->binary(), $this->node->script('face-scan.cjs')]),
			[0 => ['pipe', 'r'], 1 => ['pipe', 'w'], 2 => ['file', '/dev/null', 'a']], $this->pipes, null, $env);
		if (!is_resource($this->proc)) {
			throw new \RuntimeException('Could not start node');
		}
		$ready = $this->readLine();
		if (($ready['ready'] ?? false) !== true) {
			throw new \RuntimeException('face-scan did not start');
		}
	}

	private function stopNode(): void {
		if ($this->proc === null) {
			return;
		}
		@fclose($this->pipes[0]);
		@fclose($this->pipes[1]);
		@proc_terminate($this->proc);
		@proc_close($this->proc);
		$this->proc = null;
	}

	/** @return list<array{x: float, y: float, width: float, height: float, score: float, yaw: float, roll: float, vector: list<float>}> */
	private function detect(string $image): array {
		fwrite($this->pipes[0], $image . "\n");
		fflush($this->pipes[0]);
		$result = $this->readLine();
		if (isset($result['error'])) {
			throw new \RuntimeException('detector: ' . $result['error']);
		}
		return $result;
	}

	private function readLine(): array {
		$read = [$this->pipes[1]];
		$w = $e = null;
		if (stream_select($read, $w, $e, self::READ_TIMEOUT) !== 1) {
			$this->stopNode();
			throw new \RuntimeException('detector timed out');
		}
		$line = fgets($this->pipes[1]);
		if ($line === false) {
			$this->stopNode();
			throw new \RuntimeException('detector exited');
		}
		return json_decode($line, true, 512, JSON_THROW_ON_ERROR);
	}
}
