<?php

declare(strict_types=1);

namespace OCA\MemoriesFacetag\BackgroundJob;

use OCA\MemoriesFacetag\AppInfo\Application;
use OCA\MemoriesFacetag\Service\Scanner;
use OCP\AppFramework\Utility\ITimeFactory;
use OCP\BackgroundJob\IJob;
use OCP\BackgroundJob\TimedJob;
use OCP\IAppConfig;
use Psr\Log\LoggerInterface;

/**
 * Keeps the library scan going from Nextcloud's own cron, the way Recognize schedules its work.
 *
 * Every 10 minutes it scans for up to `scan_minutes` (default 8), so a backlog is worked through
 * in slices and, once caught up, each new photo gets its second pass a day after Recognize's first.
 * Off unless `occ config:app:set memories_facetag scan_enabled --value=yes`.
 *
 * Clustering is expensive (Recognize re-clusters all of a user's unassigned faces), so instead of
 * after every slice it is queued when the backlog is done, or at most every six hours meanwhile.
 */
class ScanJob extends TimedJob {
	private const CLUSTER_EVERY = 6 * 3600;

	public function __construct(
		ITimeFactory $time,
		private Scanner $scanner,
		private IAppConfig $appConfig,
		private LoggerInterface $logger,
	) {
		parent::__construct($time);
		$this->setInterval(600);
		$this->setTimeSensitivity(IJob::TIME_INSENSITIVE);
		$this->setAllowParallelRuns(false);
	}

	protected function run($argument): void {
		if ($this->appConfig->getValueString(Application::APP_ID, 'scan_enabled', 'no') !== 'yes') {
			return;
		}
		$minutes = max(1, $this->appConfig->getValueInt(Application::APP_ID, 'scan_minutes', 8));
		$stats = $this->scanner->scan(['deadline' => time() + 60 * $minutes, 'cluster' => false]);
		if ($stats['locked']) {
			return;
		}

		$pending = array_values(array_unique(array_merge(
			json_decode($this->appConfig->getValueString(Application::APP_ID, 'cluster_pending', '[]'), true) ?: [],
			$stats['users'],
		)));
		$last = $this->appConfig->getValueInt(Application::APP_ID, 'cluster_queued_at', 0);
		if ($pending && ($stats['remaining'] === 0 || time() - $last > self::CLUSTER_EVERY)) {
			$this->scanner->queueClustering($pending);
			$pending = [];
			$this->appConfig->setValueInt(Application::APP_ID, 'cluster_queued_at', time());
		}
		$this->appConfig->setValueString(Application::APP_ID, 'cluster_pending', json_encode($pending));

		if ($stats['photos'] > 0) {
			$this->logger->info(sprintf('facetag scan: %d photos, %d faces added (%d named), %d errors, %d left',
				$stats['photos'], $stats['added'], $stats['named'], $stats['errors'], $stats['remaining']), ['app' => Application::APP_ID]);
		}
	}
}
