<?php

declare(strict_types=1);

namespace OCA\MemoriesFacetag\Command;

use OCA\MemoriesFacetag\Service\Scanner;
use Symfony\Component\Console\Command\Command;
use Symfony\Component\Console\Input\InputInterface;
use Symfony\Component\Console\Input\InputOption;
use Symfony\Component\Console\Output\OutputInterface;

/** `occ memories_facetag:scan` — run the library scan by hand; see Scanner for what it does. */
class Scan extends Command {
	public function __construct(
		private Scanner $scanner,
	) {
		parent::__construct();
	}

	protected function configure(): void {
		$d = Scanner::DEFAULTS;
		$this->setName('memories_facetag:scan')
			->setDescription('Find faces Recognize missed and add them, naming the ones that clearly match a known person')
			->addOption('user', null, InputOption::VALUE_REQUIRED, 'Only photos this user can see')
			->addOption('path', null, InputOption::VALUE_REQUIRED, 'Only photos under this folder of --user (e.g. "Photos/2023")')
			->addOption('file', null, InputOption::VALUE_REQUIRED, 'Only this file id (scanned even if done before)')
			->addOption('limit', null, InputOption::VALUE_REQUIRED, 'Stop after this many photos', '0')
			->addOption('max-minutes', null, InputOption::VALUE_REQUIRED, 'Stop after this long', '0')
			->addOption('min-score', null, InputOption::VALUE_REQUIRED, 'Detector confidence floor', (string)$d['minScore'])
			->addOption('max-yaw', null, InputOption::VALUE_REQUIRED, 'Drop faces turned further than this', (string)$d['maxYaw'])
			->addOption('min-size', null, InputOption::VALUE_REQUIRED, 'Drop faces smaller than this fraction of the image, both ways (Recognize never clusters below 0.03)', (string)$d['minSize'])
			->addOption('assign-distance', null, InputOption::VALUE_REQUIRED, 'Name a new face directly when its nearest person is this close (0 = never; leave it to Recognize\'s clustering)', (string)$d['assignDistance'])
			->addOption('assign-margin', null, InputOption::VALUE_REQUIRED, '...and at least this much closer than the next person', (string)$d['assignMargin'])
			->addOption('dry-run', null, InputOption::VALUE_NONE, 'Report what would be added; write nothing')
			->addOption('no-cluster', null, InputOption::VALUE_NONE, 'Do not queue Recognize\'s ClusterFacesJob afterwards')
			->addOption('undo', null, InputOption::VALUE_NONE, 'Delete every face the scan named itself, and every face it added that is not now under a named person; forget which photos were scanned');
	}

	protected function execute(InputInterface $input, OutputInterface $output): int {
		if ($input->getOption('undo')) {
			$output->writeln($this->scanner->undo());
			return 0;
		}
		$minutes = (int)$input->getOption('max-minutes');
		$stats = $this->scanner->scan([
			'user' => $input->getOption('user'),
			'path' => $input->getOption('path'),
			'file' => $input->getOption('file') !== null ? (int)$input->getOption('file') : null,
			'limit' => (int)$input->getOption('limit'),
			'deadline' => $minutes > 0 ? time() + 60 * $minutes : PHP_INT_MAX,
			'minScore' => (float)$input->getOption('min-score'),
			'maxYaw' => (float)$input->getOption('max-yaw'),
			'minSize' => (float)$input->getOption('min-size'),
			'assignDistance' => (float)$input->getOption('assign-distance'),
			'assignMargin' => (float)$input->getOption('assign-margin'),
			'dryRun' => (bool)$input->getOption('dry-run'),
			'cluster' => !$input->getOption('no-cluster'),
		], static function (string $line, bool $verbose) use ($output): void {
			$output->writeln($line, $verbose ? OutputInterface::VERBOSITY_VERBOSE : OutputInterface::VERBOSITY_NORMAL);
		});
		if ($stats['locked']) {
			return 1;
		}
		$output->writeln(sprintf('<info>Done: %d photos, %d new faces, %d detection rows %s (%d named by the scan), %d errors, %d photos left%s</info>',
			$stats['photos'], $stats['found'], $stats['added'], $input->getOption('dry-run') ? 'would be added' : 'added',
			$stats['named'], $stats['errors'], $stats['remaining'],
			$stats['users'] && !$input->getOption('dry-run') && !$input->getOption('no-cluster') ? '; clustering queued for ' . implode(', ', $stats['users']) : ''));
		return 0;
	}
}
