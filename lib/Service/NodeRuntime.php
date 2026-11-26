<?php

declare(strict_types=1);

namespace OCA\MemoriesFacetag\Service;

use OCP\App\IAppManager;
use OCP\IConfig;

/** Recognize's node binary and node_modules, which the face scripts run on. */
class NodeRuntime {
	public function __construct(
		private IAppManager $appManager,
		private IConfig $config,
	) {
	}

	public function binary(): string {
		$node = $this->config->getAppValue('recognize', 'node_binary', '');
		if ($node === '' || !is_executable($node)) {
			$node = $this->appManager->getAppPath('recognize') . '/bin/node';
		}
		return $node;
	}

	public function script(string $name): string {
		return dirname(__DIR__, 2) . '/bin/' . $name;
	}

	/** @return array<string, string> */
	public function env(array $extra = []): array {
		return ['NODE_PATH' => $this->appManager->getAppPath('recognize') . '/node_modules', 'TF_CPP_MIN_LOG_LEVEL' => '2'] + $extra;
	}

	/** Prefix $command with `nice`, when there is one, so batch work yields to everything else. */
	public function niced(array $command): array {
		foreach (['/usr/bin/nice', '/bin/nice'] as $nice) {
			if (is_executable($nice)) {
				return [$nice, '-n', '19', ...$command];
			}
		}
		return $command;
	}
}
