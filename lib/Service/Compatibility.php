<?php

declare(strict_types=1);

namespace OCA\MemoriesFacetag\Service;

use OCA\MemoriesFacetag\AppInfo\Application;
use OCP\App\IAppManager;
use OCP\IAppConfig;
use OCP\IDBConnection;
use OCP\ITempManager;
use Symfony\Component\Process\Process;

/**
 * Whether the Memories and Recognize installed are ones this app is known to work with.
 *
 * Nextcloud has no way for one app to depend on versions of another, so the app checks for
 * itself. Two levels:
 *
 * - versionProblems(): cheap, used on every request. Outside the tested major versions the app
 *   switches itself off — no tag button, no API, no scan — until TESTED_MAJORS is widened after
 *   testing (or an admin sets `ignore_versions` to `yes` to try a new release).
 * - selfTest(): the things the app actually relies on, for `occ memories_facetag:check`: Recognize's
 *   tables and columns, its clustering job and its node/TensorFlow runtime (run end to end), and
 *   the Memories internals the viewer script hooks into.
 */
class Compatibility {
	/** Major versions tested; a new major switches the app off until it has been tested. */
	public const TESTED_MAJORS = [
		'memories' => [11],
		'recognize' => [12],
	];

	/** Columns this app reads or writes, by table */
	private const TABLES = [
		'recognize_face_detections' => ['id', 'user_id', 'file_id', 'x', 'y', 'width', 'height', 'cluster_id', 'threshold', 'face_vector'],
		'recognize_face_clusters' => ['id', 'title', 'user_id'],
		'recognize_queue_faces' => ['file_id'],
		'memories' => ['fileid', 'isvideo', 'orphan', 'indexed_at', 'datetaken'],
	];

	/** Strings in Memories' viewer bundle that the viewer script depends on */
	private const MEMORIES_MARKERS = [
		'_m.viewer.photoswipe=' => 'the viewer object exposing its PhotoSwipe instance (globalThis._m.viewer.photoswipe)',
		'memories-viewer' => 'the viewer element class .memories-viewer',
		'top-bar' => 'the viewer top bar .top-bar',
		'pswp__img' => 'PhotoSwipe slide content div.pswp__img',
		'_onFocusIn' => 'the PhotoSwipe focus trap the name box works around',
	];

	private ?array $versionProblems = null;

	public function __construct(
		private IAppManager $appManager,
		private IAppConfig $appConfig,
		private IDBConnection $db,
		private ITempManager $tempManager,
		private NodeRuntime $node,
	) {
	}

	/** True when the app may run: tested versions, or the admin chose to ignore the check. */
	public function ok(): bool {
		return $this->versionProblems() === [] || $this->ignoringVersions();
	}

	public function ignoringVersions(): bool {
		return $this->appConfig->getValueString(Application::APP_ID, 'ignore_versions', 'no') === 'yes';
	}

	/** @return list<string> */
	public function versionProblems(): array {
		if ($this->versionProblems !== null) {
			return $this->versionProblems;
		}
		$problems = [];
		foreach (self::TESTED_MAJORS as $app => $majors) {
			if (!$this->appManager->isEnabledForAnyone($app)) {
				$problems[] = "$app is not enabled";
				continue;
			}
			$version = $this->appManager->getAppVersion($app);
			$major = (int)explode('.', $version)[0];
			if (!in_array($major, $majors, true)) {
				$problems[] = sprintf('%s %s is not a tested version (tested: %s)', $app, $version,
					implode(', ', array_map(static fn (int $m) => "$m.x", $majors)));
			}
		}
		return $this->versionProblems = $problems;
	}

	/**
	 * Everything the app relies on, checked directly. Slow (runs the face model once).
	 *
	 * @return list<string> problems; empty when all is well
	 */
	public function selfTest(): array {
		$problems = $this->versionProblems();
		if (array_diff(array_keys(self::TESTED_MAJORS), $this->enabledApps())) {
			return $problems;
		}

		foreach (self::TABLES as $table => $columns) {
			try {
				$qb = $this->db->getQueryBuilder();
				$qb->select(...$columns)->from($table)->setMaxResults(1);
				$qb->executeQuery()->closeCursor();
			} catch (\Throwable $e) {
				$problems[] = "table $table no longer has the columns " . implode(', ', $columns) . ': ' . $e->getMessage();
			}
		}

		if (!class_exists('OCA\Recognize\BackgroundJobs\ClusterFacesJob')) {
			$problems[] = 'Recognize no longer has OCA\Recognize\BackgroundJobs\ClusterFacesJob (the scan queues it)';
		}

		$problems = array_merge($problems, $this->testFaceModel());

		$bundle = $this->appManager->getAppPath('memories') . '/js/memories-main.js';
		$js = @file_get_contents($bundle);
		if ($js === false) {
			$problems[] = "cannot read Memories' viewer bundle $bundle";
		} else {
			foreach (self::MEMORIES_MARKERS as $marker => $what) {
				if (!str_contains($js, $marker)) {
					$problems[] = "Memories' viewer bundle no longer contains \"$marker\" — $what";
				}
			}
		}
		return $problems;
	}

	/** Run the batch face script on a blank image: proves node, tfjs-node and the models load. */
	private function testFaceModel(): array {
		$image = $this->tempManager->getTemporaryFile('.jpg');
		try {
			$im = new \Imagick();
			$im->newImage(64, 64, 'gray');
			$im->setImageFormat('jpeg');
			file_put_contents($image, $im->getImageBlob());
			$process = new Process([$this->node->binary(), $this->node->script('face-scan.cjs')], null, $this->node->env());
			$process->setInput($image . "\n");
			$process->setTimeout(120);
			$process->run();
			$lines = array_values(array_filter(explode("\n", trim($process->getOutput()))));
			$ready = json_decode($lines[0] ?? '', true);
			$result = json_decode($lines[1] ?? '', true);
			if (!$process->isSuccessful() || ($ready['ready'] ?? false) !== true || !is_array($result) || isset($result['error'])) {
				// node prints a stack; the line naming the error is the useful one
				$err = trim($process->getErrorOutput() ?: $process->getOutput());
				foreach (explode("\n", $err) as $line) {
					if (preg_match('/^\w*Error\b/', trim($line))) {
						$err = trim($line);
						break;
					}
				}
				return ['Recognize\'s face model does not run for this app: ' . $err];
			}
			return [];
		} catch (\Throwable $e) {
			return ['Recognize\'s face model does not run for this app: ' . $e->getMessage()];
		} finally {
			@unlink($image);
		}
	}

	/** @return list<string> */
	private function enabledApps(): array {
		return array_values(array_filter(array_keys(self::TESTED_MAJORS), fn (string $a) => $this->appManager->isEnabledForAnyone($a)));
	}
}
