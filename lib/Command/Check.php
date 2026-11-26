<?php

declare(strict_types=1);

namespace OCA\MemoriesFacetag\Command;

use OCA\MemoriesFacetag\Service\Compatibility;
use Symfony\Component\Console\Command\Command;
use Symfony\Component\Console\Input\InputInterface;
use Symfony\Component\Console\Output\OutputInterface;

/**
 * `occ memories_facetag:check` — is everything this app relies on in Memories and Recognize still
 * there? Prints nothing and exits 0 when it is (with -q; otherwise one "OK" line), lists the
 * problems and exits 1 when not. Meant to run after app updates.
 */
class Check extends Command {
	public function __construct(
		private Compatibility $compatibility,
	) {
		parent::__construct();
	}

	protected function configure(): void {
		$this->setName('memories_facetag:check')
			->setDescription('Check that the Memories and Recognize installed still provide everything this app relies on');
	}

	protected function execute(InputInterface $input, OutputInterface $output): int {
		$problems = $this->compatibility->selfTest();
		if (!$problems) {
			$output->writeln('OK: Memories and Recognize provide everything memories_facetag relies on.');
			return 0;
		}
		$output->writeln('memories_facetag: Memories or Recognize changed in ways this app has not been tested with:', OutputInterface::VERBOSITY_QUIET);
		foreach ($problems as $p) {
			$output->writeln("- $p", OutputInterface::VERBOSITY_QUIET);
		}
		$output->writeln($this->compatibility->ok()
			? 'The app is still running (ignore_versions is set, or only non-version checks failed).'
			: 'The app has switched itself off until it is retested and Compatibility::TESTED_MAJORS widened.', OutputInterface::VERBOSITY_QUIET);
		return 1;
	}
}
