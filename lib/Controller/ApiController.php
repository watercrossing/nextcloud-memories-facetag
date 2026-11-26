<?php

declare(strict_types=1);

namespace OCA\MemoriesFacetag\Controller;

use OCA\MemoriesFacetag\AppInfo\Application;
use OCA\MemoriesFacetag\Service\FaceLocator;
use OCA\MemoriesFacetag\Service\FaceStore;
use OCP\AppFramework\Controller;
use OCP\AppFramework\Http;
use OCP\AppFramework\Http\Attribute\NoAdminRequired;
use OCP\AppFramework\Http\JSONResponse;
use OCP\Files\File;
use OCP\Files\IRootFolder;
use OCP\ICache;
use OCP\ICacheFactory;
use OCP\IRequest;
use OCP\IUserSession;
use OCP\Security\ISecureRandom;
use Psr\Log\LoggerInterface;

class ApiController extends Controller {
	/** How long a located face's descriptor is kept, waiting for the user to pick a name */
	private const LOCATE_TTL = 1800;

	private ICache $cache;

	public function __construct(
		IRequest $request,
		private IUserSession $userSession,
		private IRootFolder $rootFolder,
		private FaceStore $store,
		private FaceLocator $locator,
		private ISecureRandom $random,
		private LoggerInterface $logger,
		ICacheFactory $cacheFactory,
	) {
		parent::__construct(Application::APP_ID, $request);
		$this->cache = $cacheFactory->createDistributed(Application::APP_ID);
	}

	/** Faces already on a photo, plus the user's named people for the picker. */
	#[NoAdminRequired]
	public function file(int $fileId): JSONResponse {
		$uid = $this->uid();
		if ($this->userFile($uid, $fileId) === null) {
			return $this->error('File not found', Http::STATUS_NOT_FOUND);
		}
		return new JSONResponse([
			'detections' => $this->store->detectionsForFile($uid, $fileId),
			'people' => $this->store->people($uid),
		]);
	}

	/** Look for a face under the box the user placed, and suggest who it is. */
	#[NoAdminRequired]
	public function locate(int $fileId, float $x, float $y, float $width, float $height): JSONResponse {
		$uid = $this->uid();
		$file = $this->userFile($uid, $fileId);
		if ($file === null) {
			return $this->error('File not found', Http::STATUS_NOT_FOUND);
		}
		if (!self::validBox($x, $y, $width, $height)) {
			return $this->error('Invalid box', Http::STATUS_BAD_REQUEST);
		}
		try {
			$face = $this->locator->locate($file, $x, $y, $width, $height);
		} catch (\Throwable $e) {
			$this->logger->error('Locating face failed', ['app' => Application::APP_ID, 'exception' => $e]);
			// Tagging still works without detection: the user picks from the plain list
			return new JSONResponse(['face' => null, 'token' => null, 'suggestions' => [], 'error' => 'Face detection failed']);
		}
		if ($face === null) {
			return new JSONResponse(['face' => null, 'token' => null, 'suggestions' => []]);
		}
		$token = $this->random->generate(24, ISecureRandom::CHAR_ALPHANUMERIC);
		$this->cache->set($this->cacheKey($uid, $fileId, $token), json_encode($face), self::LOCATE_TTL);
		$vector = $face['vector'];
		unset($face['vector']);
		return new JSONResponse([
			'face' => $face,
			'token' => $token,
			'suggestions' => $this->store->suggest($uid, $vector, 5, null, $fileId),
		]);
	}

	/**
	 * Tag a person on a photo by hand.
	 *
	 * Either $clusterId (an existing person) or $name (existing or new) names them.
	 * If $token refers to a face found by locate(), its box and descriptor are used.
	 */
	#[NoAdminRequired]
	public function addTag(int $fileId, float $x, float $y, float $width, float $height,
		?string $token = null, ?int $clusterId = null, ?string $name = null): JSONResponse {
		$uid = $this->uid();
		if ($this->userFile($uid, $fileId) === null) {
			return $this->error('File not found', Http::STATUS_NOT_FOUND);
		}
		$clusterId = $this->resolvePerson($uid, $clusterId, $name);
		if ($clusterId === null) {
			return $this->error('Unknown person', Http::STATUS_BAD_REQUEST);
		}

		$box = ['x' => $x, 'y' => $y, 'width' => $width, 'height' => $height];
		$vector = null;
		if ($token !== null && $token !== '') {
			$cached = $this->cache->get($this->cacheKey($uid, $fileId, $token));
			$face = is_string($cached) ? json_decode($cached, true) : null;
			if (is_array($face) && is_array($face['vector'] ?? null)) {
				$vector = array_map('floatval', $face['vector']);
				$box = ['x' => (float)$face['x'], 'y' => (float)$face['y'], 'width' => (float)$face['width'], 'height' => (float)$face['height']];
			}
		}
		if (!self::validBox($box['x'], $box['y'], $box['width'], $box['height'])) {
			return $this->error('Invalid box', Http::STATUS_BAD_REQUEST);
		}
		$id = $this->store->insertManual($uid, $fileId, $box, $vector, $clusterId);
		return new JSONResponse(['id' => $id, 'detections' => $this->store->detectionsForFile($uid, $fileId)]);
	}

	/** Suggestions for a face already in the database (e.g. one Recognize found but nobody named). */
	#[NoAdminRequired]
	public function suggestForDetection(int $id): JSONResponse {
		$uid = $this->uid();
		$detection = $this->ownDetection($uid, $id);
		if ($detection === null) {
			return $this->error('Face not found', Http::STATUS_NOT_FOUND);
		}
		$vector = json_decode($detection['face_vector'], true);
		if (!is_array($vector) || !array_filter($vector)) {
			return new JSONResponse(['suggestions' => []]);
		}
		return new JSONResponse(['suggestions' => $this->store->suggest($uid, array_map('floatval', $vector), 5, $id, $detection['file_id'])]);
	}

	/** Put an existing face under a (possibly new) person. */
	#[NoAdminRequired]
	public function assign(int $id, ?int $clusterId = null, ?string $name = null): JSONResponse {
		$uid = $this->uid();
		$detection = $this->ownDetection($uid, $id);
		if ($detection === null) {
			return $this->error('Face not found', Http::STATUS_NOT_FOUND);
		}
		$clusterId = $this->resolvePerson($uid, $clusterId, $name);
		if ($clusterId === null) {
			return $this->error('Unknown person', Http::STATUS_BAD_REQUEST);
		}
		$this->store->assign($id, $clusterId);
		return new JSONResponse(['detections' => $this->store->detectionsForFile($uid, $detection['file_id'])]);
	}

	#[NoAdminRequired]
	public function remove(int $id): JSONResponse {
		$uid = $this->uid();
		$detection = $this->ownDetection($uid, $id);
		if ($detection === null) {
			return $this->error('Face not found', Http::STATUS_NOT_FOUND);
		}
		$this->store->remove($detection);
		return new JSONResponse(['detections' => $this->store->detectionsForFile($uid, $detection['file_id'])]);
	}

	private function uid(): string {
		$user = $this->userSession->getUser();
		if ($user === null) {
			throw new \RuntimeException('Not logged in');
		}
		return $user->getUID();
	}

	private function userFile(string $uid, int $fileId): ?File {
		$node = $this->rootFolder->getUserFolder($uid)->getFirstNodeById($fileId);
		return $node instanceof File ? $node : null;
	}

	/** A detection owned by $uid on a file $uid can still see. */
	private function ownDetection(string $uid, int $id): ?array {
		$detection = $this->store->getDetection($id);
		if ($detection === null || $detection['user_id'] !== $uid) {
			return null;
		}
		return $this->userFile($uid, $detection['file_id']) === null ? null : $detection;
	}

	private function resolvePerson(string $uid, ?int $clusterId, ?string $name): ?int {
		if ($clusterId !== null && $clusterId > 0) {
			return $this->store->clusterBelongsTo($clusterId, $uid) ? $clusterId : null;
		}
		$name = trim((string)$name);
		if ($name === '' || mb_strlen($name) > 200) {
			return null;
		}
		return $this->store->clusterForName($uid, $name);
	}

	private function cacheKey(string $uid, int $fileId, string $token): string {
		return 'locate/' . $uid . '/' . $fileId . '/' . $token;
	}

	private static function validBox(float $x, float $y, float $w, float $h): bool {
		return $w > 0 && $h > 0 && $w <= 1 && $h <= 1
			&& $x >= -0.01 && $y >= -0.01 && $x + $w <= 1.01 && $y + $h <= 1.01;
	}

	private function error(string $message, int $status): JSONResponse {
		return new JSONResponse(['message' => $message], $status);
	}
}
