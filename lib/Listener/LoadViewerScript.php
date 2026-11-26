<?php

declare(strict_types=1);

namespace OCA\MemoriesFacetag\Listener;

use OCA\MemoriesFacetag\AppInfo\Application;
use OCA\MemoriesFacetag\Service\Compatibility;
use OCP\AppFramework\Http\Events\BeforeTemplateRenderedEvent;
use OCP\EventDispatcher\Event;
use OCP\EventDispatcher\IEventListener;
use OCP\Util;

/**
 * Loads the tagging script into Memories' own pages, for logged-in users only.
 *
 * @template-implements IEventListener<BeforeTemplateRenderedEvent>
 */
class LoadViewerScript implements IEventListener {
	public function __construct(
		private Compatibility $compatibility,
	) {
	}

	public function handle(Event $event): void {
		if (!$event instanceof BeforeTemplateRenderedEvent || !$event->isLoggedIn()) {
			return;
		}
		if ($event->getResponse()->getApp() !== 'memories' || !$this->compatibility->ok()) {
			return;
		}
		Util::addScript(Application::APP_ID, 'facetag');
		Util::addStyle(Application::APP_ID, 'facetag');
	}
}
