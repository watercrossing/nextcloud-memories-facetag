<?php

declare(strict_types=1);

namespace OCA\MemoriesFacetag\Service;

use OCP\Files\File;
use Psr\Log\LoggerInterface;
use Symfony\Component\Process\Process;

/** Runs Recognize's face models on the area a user clicked. */
class FaceLocator {
	private const TIMEOUT_SECONDS = 60;

	public function __construct(
		private PreviewImage $previewImage,
		private NodeRuntime $node,
		private LoggerInterface $logger,
	) {
	}

	/**
	 * @return array{x: float, y: float, width: float, height: float, score: float, vector: list<float>}|null
	 */
	public function locate(File $file, float $x, float $y, float $w, float $h): ?array {
		$image = $this->previewImage->write($file);
		try {
			$process = new Process(
				[$this->node->binary(), $this->node->script('face-locate.cjs'), $image, (string)$x, (string)$y, (string)$w, (string)$h],
				null,
				$this->node->env(),
			);
			$process->setTimeout(self::TIMEOUT_SECONDS);
			$process->run();
			if (!$process->isSuccessful()) {
				$this->logger->error('face-locate failed: ' . trim($process->getErrorOutput()), ['app' => 'memories_facetag']);
				throw new \RuntimeException('Face detection failed');
			}
			$result = json_decode($process->getOutput(), true, 512, JSON_THROW_ON_ERROR);
			return is_array($result['face'] ?? null) ? $result['face'] : null;
		} finally {
			@unlink($image);
		}
	}
}
