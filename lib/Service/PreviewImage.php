<?php

declare(strict_types=1);

namespace OCA\MemoriesFacetag\Service;

use OCP\Files\File;
use OCP\IPreview;
use OCP\ITempManager;

/**
 * A ≤2048px JPEG of a photo, upright, written to a temp file for the node face scripts.
 *
 * JPEG and PNG originals are decoded directly (libjpeg decodes at reduced scale, so this is quick)
 * and turned upright by their EXIF orientation, as Nextcloud's previews and therefore Memories are.
 * Anything else comes from a Nextcloud preview that is not stored: a library scan must not leave
 * tens of gigabytes of extra previews behind.
 */
class PreviewImage {
	public const SIZE = 2048;
	private const DIRECT = ['image/jpeg', 'image/png'];

	public function __construct(
		private IPreview $preview,
		private ITempManager $tempManager,
	) {
	}

	/** Write the image to a temp file and return its path; the caller unlinks it. */
	public function write(File $file): string {
		$data = null;
		if (in_array($file->getMimeType(), self::DIRECT, true)) {
			try {
				$data = $this->fromOriginal($file);
			} catch (\Throwable $e) {
				$data = null;
			}
		}
		$data ??= $this->fromPreview($file);
		$path = $this->tempManager->getTemporaryFile('.jpg');
		if ($path === false || file_put_contents($path, $data) === false) {
			throw new \RuntimeException('Could not write temporary image');
		}
		return $path;
	}

	private function fromOriginal(File $file): string {
		$stream = $file->fopen('r');
		if ($stream === false) {
			throw new \RuntimeException('Could not open file');
		}
		$im = new \Imagick();
		try {
			// Let libjpeg decode at reduced scale instead of decoding every pixel and then shrinking
			$im->setOption('jpeg:size', self::SIZE . 'x' . self::SIZE);
			$im->readImageFile($stream);
			$im->setIteratorIndex(0);
			$im->autoOrient();
			return $this->finish($im);
		} finally {
			$im->clear();
			if (is_resource($stream)) {
				fclose($stream);
			}
		}
	}

	private function fromPreview(File $file): string {
		$preview = $this->preview->getPreview($file, self::SIZE, self::SIZE, false, IPreview::MODE_FILL, null, false);
		$im = new \Imagick();
		try {
			$im->setOption('jpeg:size', self::SIZE . 'x' . self::SIZE);
			$im->readImageBlob($preview->getContent());
			return $this->finish($im);
		} finally {
			$im->clear();
		}
	}

	/** Shrink to fit SIZE and encode as JPEG (tfjs-node decodes only JPEG/PNG/GIF/BMP). */
	private function finish(\Imagick $im): string {
		if (max($im->getImageWidth(), $im->getImageHeight()) > self::SIZE) {
			$im->thumbnailImage(self::SIZE, self::SIZE, true);
		}
		$im->setImageColorspace(\Imagick::COLORSPACE_SRGB);
		$im->stripImage();
		$im->setImageFormat('jpeg');
		$im->setImageCompressionQuality(92);
		return $im->getImageBlob();
	}
}
