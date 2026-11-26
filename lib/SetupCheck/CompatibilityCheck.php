<?php

declare(strict_types=1);

namespace OCA\MemoriesFacetag\SetupCheck;

use OCA\MemoriesFacetag\Service\Compatibility;
use OCP\SetupCheck\ISetupCheck;
use OCP\SetupCheck\SetupResult;

/** Shows on the admin overview when the app has switched itself off over untested versions. */
class CompatibilityCheck implements ISetupCheck {
	public function __construct(
		private Compatibility $compatibility,
	) {
	}

	public function getCategory(): string {
		return 'system';
	}

	public function getName(): string {
		return 'Memories manual face tags';
	}

	public function run(): SetupResult {
		$problems = $this->compatibility->versionProblems();
		if (!$problems) {
			return SetupResult::success('Memories and Recognize are tested versions.');
		}
		if ($this->compatibility->ignoringVersions()) {
			return SetupResult::warning('Running on untested versions because ignore_versions is set: ' . implode('; ', $problems) . '.');
		}
		return SetupResult::warning('Switched off: ' . implode('; ', $problems)
			. '. Run "occ memories_facetag:check"; to try it anyway, "occ config:app:set memories_facetag ignore_versions --value=yes".');
	}
}
