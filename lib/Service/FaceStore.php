<?php

declare(strict_types=1);

namespace OCA\MemoriesFacetag\Service;

use OCP\DB\QueryBuilder\IQueryBuilder;
use OCP\IDBConnection;

/**
 * Reads and writes Recognize's face tables directly.
 *
 * Recognize has no public PHP API for this, so the column layout of
 * recognize_face_detections / recognize_face_clusters is a dependency:
 * re-check it when Recognize has a major release.
 *
 * Coordinates are relative to the (rotated) image, 0..1, exactly as Recognize stores them.
 */
class FaceStore {
	/** Newest detections per person used as references for suggestions */
	private const SAMPLE_PER_PERSON = 60;
	/** A person's score is the mean distance to this many of their nearest reference faces */
	private const KNN = 5;
	/** Who else was about: photos this far either side of the one being tagged count as context */
	private const CONTEXT_SECONDS = 2 * 86400;
	/** Score bonus per ln(1 + photos nearby); tuned on 1,500 named faces (0.02 and 0.08 did worse) */
	private const CONTEXT_WEIGHT = 0.04;

	public function __construct(
		private IDBConnection $db,
	) {
	}

	/** @return list<array{id: int, x: float, y: float, width: float, height: float, clusterId: int|null, name: string, manual: bool}> */
	public function detectionsForFile(string $uid, int $fileId): array {
		$qb = $this->db->getQueryBuilder();
		$qb->select('d.id', 'd.x', 'd.y', 'd.width', 'd.height', 'd.cluster_id', 'c.title', 'm.source')
			->from('recognize_face_detections', 'd')
			->leftJoin('d', 'recognize_face_clusters', 'c', $qb->expr()->eq('c.id', 'd.cluster_id'))
			->leftJoin('d', 'facetag_manual', 'm', $qb->expr()->eq('m.detection_id', 'd.id'))
			->where($qb->expr()->eq('d.user_id', $qb->createNamedParameter($uid)))
			->andWhere($qb->expr()->eq('d.file_id', $qb->createNamedParameter($fileId, IQueryBuilder::PARAM_INT)))
			->orderBy('d.x');
		$rows = $qb->executeQuery()->fetchAll();
		return array_map(static fn (array $r) => [
			'id' => (int)$r['id'],
			'x' => (float)$r['x'],
			'y' => (float)$r['y'],
			'width' => (float)$r['width'],
			'height' => (float)$r['height'],
			'clusterId' => $r['cluster_id'] !== null && (int)$r['cluster_id'] > 0 ? (int)$r['cluster_id'] : null,
			'name' => (string)($r['title'] ?? ''),
			'manual' => $r['source'] === 'manual',
		], $rows);
	}

	/** Named people of $uid, most-photographed first. @return list<array{id: int, name: string, count: int}> */
	public function people(string $uid): array {
		$qb = $this->db->getQueryBuilder();
		$qb->select('c.id', 'c.title')
			->selectAlias($qb->func()->count('d.id'), 'n')
			->from('recognize_face_clusters', 'c')
			->leftJoin('c', 'recognize_face_detections', 'd', $qb->expr()->eq('d.cluster_id', 'c.id'))
			->where($qb->expr()->eq('c.user_id', $qb->createNamedParameter($uid)))
			->andWhere($qb->expr()->neq('c.title', $qb->createNamedParameter('')))
			->groupBy('c.id', 'c.title')
			->orderBy('n', 'DESC')
			->addOrderBy('c.title');
		return array_map(static fn (array $r) => [
			'id' => (int)$r['id'],
			'name' => (string)$r['title'],
			'count' => (int)$r['n'],
		], $qb->executeQuery()->fetchAll());
	}

	/** @return array{id: int, user_id: string, file_id: int, cluster_id: int|null, face_vector: string, manual: bool}|null  manual = placed by hand, not by Recognize or the scan */
	public function getDetection(int $id): ?array {
		$qb = $this->db->getQueryBuilder();
		$qb->select('d.id', 'd.user_id', 'd.file_id', 'd.cluster_id', 'd.face_vector', 'm.source')
			->from('recognize_face_detections', 'd')
			->leftJoin('d', 'facetag_manual', 'm', $qb->expr()->eq('m.detection_id', 'd.id'))
			->where($qb->expr()->eq('d.id', $qb->createNamedParameter($id, IQueryBuilder::PARAM_INT)));
		$r = $qb->executeQuery()->fetch();
		if ($r === false) {
			return null;
		}
		return [
			'id' => (int)$r['id'],
			'user_id' => (string)$r['user_id'],
			'file_id' => (int)$r['file_id'],
			'cluster_id' => $r['cluster_id'] !== null ? (int)$r['cluster_id'] : null,
			'face_vector' => (string)$r['face_vector'],
			'manual' => $r['source'] === 'manual',
		];
	}

	public function clusterBelongsTo(int $clusterId, string $uid): bool {
		$qb = $this->db->getQueryBuilder();
		$qb->select('id')
			->from('recognize_face_clusters')
			->where($qb->expr()->eq('id', $qb->createNamedParameter($clusterId, IQueryBuilder::PARAM_INT)))
			->andWhere($qb->expr()->eq('user_id', $qb->createNamedParameter($uid)));
		return $qb->executeQuery()->fetch() !== false;
	}

	/** The id of $uid's person called $name (case-insensitive), creating them if needed. */
	public function clusterForName(string $uid, string $name): int {
		$name = trim($name);
		$qb = $this->db->getQueryBuilder();
		$qb->select('id')
			->from('recognize_face_clusters')
			->where($qb->expr()->eq('user_id', $qb->createNamedParameter($uid)))
			->andWhere($qb->expr()->eq($qb->func()->lower('title'), $qb->createNamedParameter(mb_strtolower($name))))
			->orderBy('id')
			->setMaxResults(1);
		$id = $qb->executeQuery()->fetchOne();
		if ($id !== false) {
			return (int)$id;
		}
		$qb = $this->db->getQueryBuilder();
		$qb->insert('recognize_face_clusters')
			->values([
				'title' => $qb->createNamedParameter($name),
				'user_id' => $qb->createNamedParameter($uid),
			])
			->executeStatement();
		return $qb->getLastInsertId();
	}

	/**
	 * Insert a hand-placed face.
	 *
	 * @param list<float>|null $vector descriptor of the face found at the spot, or null if none was found
	 */
	public function insertManual(string $uid, int $fileId, array $box, ?array $vector, int $clusterId): int {
		$foundFace = $vector !== null;
		// Without a real descriptor, use the person's centroid: it sits where Recognize already
		// expects that person, so it neither drags their cluster nor attracts strangers.
		$vector ??= $this->centroid($clusterId) ?? array_fill(0, 128, 0.0);
		return $this->insert($uid, $fileId, $box, $vector, $clusterId, 'manual', $foundFace);
	}

	/**
	 * Insert a face the library scan found: under $clusterId if the scan was sure who it is,
	 * otherwise unassigned, for Recognize's clustering to place.
	 *
	 * @param list<float> $vector
	 */
	public function insertScanned(string $uid, int $fileId, array $box, array $vector, ?int $clusterId = null): int {
		// 'scanknn': the scan itself named it, from a confident nearest-faces match
		return $this->insert($uid, $fileId, $box, $vector, $clusterId, $clusterId === null ? 'scan' : 'scanknn', true);
	}

	private function insert(string $uid, int $fileId, array $box, array $vector, ?int $clusterId, string $source, bool $foundFace): int {
		$this->db->beginTransaction();
		try {
			$qb = $this->db->getQueryBuilder();
			$qb->insert('recognize_face_detections')
				->values([
					'user_id' => $qb->createNamedParameter($uid),
					'file_id' => $qb->createNamedParameter($fileId, IQueryBuilder::PARAM_INT),
					'x' => $qb->createNamedParameter($box['x']),
					'y' => $qb->createNamedParameter($box['y']),
					'width' => $qb->createNamedParameter($box['width']),
					'height' => $qb->createNamedParameter($box['height']),
					'cluster_id' => $clusterId === null
						? $qb->createNamedParameter(null, IQueryBuilder::PARAM_NULL)
						: $qb->createNamedParameter($clusterId, IQueryBuilder::PARAM_INT),
					'threshold' => $qb->createNamedParameter(0.0),
					'face_vector' => $qb->createNamedParameter(json_encode(array_values($vector), JSON_THROW_ON_ERROR)),
				])
				->executeStatement();
			$id = $qb->getLastInsertId();

			$qb = $this->db->getQueryBuilder();
			$qb->insert('facetag_manual')
				->values([
					'detection_id' => $qb->createNamedParameter($id, IQueryBuilder::PARAM_INT),
					'user_id' => $qb->createNamedParameter($uid),
					'file_id' => $qb->createNamedParameter($fileId, IQueryBuilder::PARAM_INT),
					'found_face' => $qb->createNamedParameter($foundFace, IQueryBuilder::PARAM_BOOL),
					'source' => $qb->createNamedParameter($source),
					'created_at' => $qb->createNamedParameter(time(), IQueryBuilder::PARAM_INT),
				])
				->executeStatement();
			$this->db->commit();
			return $id;
		} catch (\Throwable $e) {
			$this->db->rollBack();
			throw $e;
		}
	}

	public function assign(int $detectionId, int $clusterId): void {
		$qb = $this->db->getQueryBuilder();
		$qb->update('recognize_face_detections')
			->set('cluster_id', $qb->createNamedParameter($clusterId, IQueryBuilder::PARAM_INT))
			->set('threshold', $qb->createNamedParameter(0.0))
			->where($qb->expr()->eq('id', $qb->createNamedParameter($detectionId, IQueryBuilder::PARAM_INT)))
			->executeStatement();
	}

	/**
	 * Remove a tag.
	 *
	 * Hand-placed faces, and detected faces (Recognize's or the scan's) nobody had named, are deleted.
	 * A detected face that was in a cluster is taken out of it the way Recognize's own
	 * "remove from person" does: cluster cleared, and a threshold set at its distance from that
	 * cluster's centroid so the next clustering run doesn't put it straight back.
	 */
	public function remove(array $detection): void {
		$clusterId = $detection['cluster_id'];
		if ($detection['manual'] || $clusterId === null || $clusterId <= 0) {
			$this->deleteDetection($detection['id']);
			return;
		}
		$threshold = 0.0;
		$centroid = $this->centroid($clusterId);
		$vector = json_decode($detection['face_vector'], true);
		if ($centroid !== null && is_array($vector) && count($vector) === count($centroid)) {
			$threshold = self::distance($centroid, $vector);
		}
		$qb = $this->db->getQueryBuilder();
		$qb->update('recognize_face_detections')
			->set('cluster_id', $qb->createNamedParameter(null, IQueryBuilder::PARAM_NULL))
			->set('threshold', $qb->createNamedParameter($threshold))
			->where($qb->expr()->eq('id', $qb->createNamedParameter($detection['id'], IQueryBuilder::PARAM_INT)))
			->executeStatement();
	}

	private function deleteDetection(int $id): void {
		$this->db->beginTransaction();
		try {
			foreach (['recognize_face_detections' => 'id', 'facetag_manual' => 'detection_id'] as $table => $col) {
				$qb = $this->db->getQueryBuilder();
				$qb->delete($table)
					->where($qb->expr()->eq($col, $qb->createNamedParameter($id, IQueryBuilder::PARAM_INT)))
					->executeStatement();
			}
			$this->db->commit();
		} catch (\Throwable $e) {
			$this->db->rollBack();
			throw $e;
		}
	}

	/** @return list<float>|null */
	private function centroid(int $clusterId): ?array {
		$qb = $this->db->getQueryBuilder();
		$qb->select('face_vector')
			->from('recognize_face_detections')
			->where($qb->expr()->eq('cluster_id', $qb->createNamedParameter($clusterId, IQueryBuilder::PARAM_INT)))
			->orderBy('id', 'DESC')
			->setMaxResults(500);
		$sum = null;
		$n = 0;
		foreach ($qb->executeQuery()->fetchAll(\PDO::FETCH_COLUMN) as $json) {
			$v = json_decode((string)$json, true);
			if (!is_array($v) || self::isZero($v)) {
				continue;
			}
			if ($sum === null) {
				$sum = array_fill(0, count($v), 0.0);
			}
			if (count($v) !== count($sum)) {
				continue;
			}
			foreach ($v as $i => $f) {
				$sum[$i] += (float)$f;
			}
			$n++;
		}
		if ($sum === null || $n === 0) {
			return null;
		}
		return array_map(static fn (float $s) => $s / $n, $sum);
	}

	/**
	 * Rank $uid's named people for a face on photo $fileId.
	 *
	 * By face first, then by who else was about: each person gets a bonus for the photos within
	 * CONTEXT_SECONDS either side that they are tagged in, and people already tagged on this photo
	 * are left out (they can't be this face too). On 1,500 named faces (2026-11-26, faces the scan
	 * added excluded) this took the right person first from 94.2% to 96.4% of the time, and on the
	 * 370 where the nearest person was further than 0.42 — where suggestions matter — from 82% to 90%.
	 *
	 * @param list<float> $vector
	 * @return list<array{id: int, name: string, distance: float, score: float, nearby: int}>
	 */
	public function suggest(string $uid, array $vector, int $limit = 5, ?int $excludeDetection = null, ?int $fileId = null): array {
		$context = $fileId !== null ? $this->contextCounts($uid, $fileId) : [];
		$exclude = $fileId !== null ? $this->peopleOnFile($uid, $fileId, $excludeDetection) : [];
		return self::rank($vector, $this->referenceFaces($uid, $excludeDetection), $limit, $context, $exclude);
	}

	/**
	 * How many photos within CONTEXT_SECONDS of $fileId's date each named person is tagged in,
	 * counting only faces Recognize found or a person tagged — not the scan's (see referenceFaces).
	 *
	 * @return array<int, int> cluster id => photos
	 */
	public function contextCounts(string $uid, int $fileId): array {
		$qb = $this->db->getQueryBuilder();
		$qb->select('datetaken')->from('memories')
			->where($qb->expr()->eq('fileid', $qb->createNamedParameter($fileId, IQueryBuilder::PARAM_INT)));
		$taken = $qb->executeQuery()->fetchOne();
		if (!is_string($taken) || $taken === '') {
			return [];
		}
		try {
			$t = new \DateTimeImmutable($taken, new \DateTimeZone('UTC'));
		} catch (\Exception $e) {
			return [];
		}
		$qb = $this->db->getQueryBuilder();
		$qb->select('d.cluster_id')
			->selectAlias($qb->func()->count($qb->createFunction('DISTINCT d.file_id')), 'n')
			->from('recognize_face_detections', 'd')
			->innerJoin('d', 'recognize_face_clusters', 'c', $qb->expr()->eq('c.id', 'd.cluster_id'))
			->innerJoin('d', 'memories', 'm', $qb->expr()->eq('m.fileid', 'd.file_id'))
			->leftJoin('d', 'facetag_manual', 'fm', $qb->expr()->eq('fm.detection_id', 'd.id'))
			->where($qb->expr()->eq('d.user_id', $qb->createNamedParameter($uid)))
			->andWhere($qb->expr()->orX($qb->expr()->isNull('fm.detection_id'), $qb->expr()->eq('fm.source', $qb->createNamedParameter('manual'))))
			->andWhere($qb->expr()->neq('c.title', $qb->createNamedParameter('')))
			->andWhere($qb->expr()->neq('d.file_id', $qb->createNamedParameter($fileId, IQueryBuilder::PARAM_INT)))
			->andWhere($qb->expr()->gte('m.datetaken', $qb->createNamedParameter($t->modify('-' . self::CONTEXT_SECONDS . ' seconds')->format('Y-m-d H:i:s'))))
			->andWhere($qb->expr()->lte('m.datetaken', $qb->createNamedParameter($t->modify('+' . self::CONTEXT_SECONDS . ' seconds')->format('Y-m-d H:i:s'))))
			->groupBy('d.cluster_id');
		$out = [];
		foreach ($qb->executeQuery()->fetchAll() as $r) {
			$out[(int)$r['cluster_id']] = (int)$r['n'];
		}
		return $out;
	}

	/**
	 * Named people already tagged on $fileId for $uid, apart from detection $exceptDetection.
	 *
	 * @return list<int> cluster ids
	 */
	public function peopleOnFile(string $uid, int $fileId, ?int $exceptDetection = null): array {
		$qb = $this->db->getQueryBuilder();
		$qb->selectDistinct('d.cluster_id')
			->from('recognize_face_detections', 'd')
			->innerJoin('d', 'recognize_face_clusters', 'c', $qb->expr()->eq('c.id', 'd.cluster_id'))
			->where($qb->expr()->eq('d.user_id', $qb->createNamedParameter($uid)))
			->andWhere($qb->expr()->eq('d.file_id', $qb->createNamedParameter($fileId, IQueryBuilder::PARAM_INT)))
			->andWhere($qb->expr()->neq('c.title', $qb->createNamedParameter('')))
			->andWhere($qb->expr()->neq('d.id', $qb->createNamedParameter($exceptDetection ?? -1, IQueryBuilder::PARAM_INT)));
		return array_map('intval', $qb->executeQuery()->fetchAll(\PDO::FETCH_COLUMN));
	}

	/**
	 * The faces suggestions are measured against: the newest SAMPLE_PER_PERSON faces of each named
	 * person that Recognize found or a person tagged by hand.
	 *
	 * Faces the library scan added are left out, whoever named them: they are inferences, and
	 * measuring new faces against them feeds back — in November 2026 one person's group grew from
	 * 30 faces to 399 that way, nearly all of them strangers. Hand-placed tags with no face under
	 * them carry a stand-in vector, so they are no evidence either.
	 *
	 * @return array{names: array<int, string>, vectors: array<int, list<list<float>>>}
	 */
	public function referenceFaces(string $uid, ?int $excludeDetection = null): array {
		$sql = 'SELECT s.cluster_id, s.title, s.face_vector FROM (
				SELECT d.cluster_id, c.title, d.face_vector,
					ROW_NUMBER() OVER (PARTITION BY d.cluster_id ORDER BY d.id DESC) AS rn
				FROM *PREFIX*recognize_face_detections d
				JOIN *PREFIX*recognize_face_clusters c ON c.id = d.cluster_id
				LEFT JOIN *PREFIX*facetag_manual m ON m.detection_id = d.id
				WHERE c.user_id = ? AND c.title <> \'\' AND d.user_id = ?
					AND (m.detection_id IS NULL OR (m.source = \'manual\' AND m.found_face = ?))
					AND d.id <> ?
			) s WHERE s.rn <= ?';
		$result = $this->db->executeQuery($sql, [$uid, $uid, true, $excludeDetection ?? -1, self::SAMPLE_PER_PERSON], [
			IQueryBuilder::PARAM_STR, IQueryBuilder::PARAM_STR, IQueryBuilder::PARAM_BOOL, IQueryBuilder::PARAM_INT, IQueryBuilder::PARAM_INT,
		]);
		$names = [];
		$vectors = [];
		while (($row = $result->fetch()) !== false) {
			$v = json_decode((string)$row['face_vector'], true);
			if (!is_array($v) || self::isZero($v)) {
				continue;
			}
			$cid = (int)$row['cluster_id'];
			$names[$cid] = (string)$row['title'];
			$vectors[$cid][] = array_map('floatval', $v);
		}
		$result->closeCursor();
		return ['names' => $names, 'vectors' => $vectors];
	}

	/**
	 * Rank people for a face, best first.
	 *
	 * `distance` is the mean distance to the person's KNN nearest reference faces; `score`, which
	 * orders the list, takes CONTEXT_WEIGHT × ln(1 + photos nearby) off it. People in $exclude are
	 * left out.
	 *
	 * @param list<float> $vector
	 * @param array{names: array<int, string>, vectors: array<int, list<list<float>>>} $refs
	 * @param array<int, int> $context cluster id => photos nearby in time
	 * @param list<int> $exclude cluster ids
	 * @return list<array{id: int, name: string, distance: float, score: float, nearby: int}>
	 */
	public static function rank(array $vector, array $refs, int $limit = 5, array $context = [], array $exclude = []): array {
		$distances = [];
		$scores = [];
		$excluded = array_flip($exclude);
		foreach ($refs['vectors'] as $cid => $faces) {
			if (isset($excluded[$cid])) {
				continue;
			}
			$ds = [];
			foreach ($faces as $v) {
				if (count($v) === count($vector)) {
					$ds[] = self::distance($vector, $v);
				}
			}
			if (!$ds) {
				continue;
			}
			sort($ds);
			$nearest = array_slice($ds, 0, self::KNN);
			$distances[$cid] = array_sum($nearest) / count($nearest);
			$scores[$cid] = $distances[$cid] - self::CONTEXT_WEIGHT * log(1 + ($context[$cid] ?? 0));
		}
		asort($scores);
		$out = [];
		foreach (array_slice($scores, 0, $limit, true) as $cid => $score) {
			$out[] = [
				'id' => $cid,
				'name' => $refs['names'][$cid],
				'distance' => round($distances[$cid], 3),
				'score' => round($score, 3),
				'nearby' => $context[$cid] ?? 0,
			];
		}
		return $out;
	}

	private static function distance(array $a, array $b): float {
		$sum = 0.0;
		foreach ($a as $i => $x) {
			$d = $x - $b[$i];
			$sum += $d * $d;
		}
		return sqrt($sum);
	}

	private static function isZero(array $v): bool {
		foreach ($v as $x) {
			if ($x != 0) {
				return false;
			}
		}
		return true;
	}

	/** Every detection box on a file, any user's. @return list<array{x: float, y: float, width: float, height: float}> */
	public function boxesForFile(int $fileId): array {
		$qb = $this->db->getQueryBuilder();
		$qb->select('x', 'y', 'width', 'height')
			->from('recognize_face_detections')
			->where($qb->expr()->eq('file_id', $qb->createNamedParameter($fileId, IQueryBuilder::PARAM_INT)));
		return array_map(static fn (array $r) => [
			'x' => (float)$r['x'], 'y' => (float)$r['y'], 'width' => (float)$r['width'], 'height' => (float)$r['height'],
		], $qb->executeQuery()->fetchAll());
	}
}
