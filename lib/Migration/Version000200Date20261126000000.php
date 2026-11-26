<?php

declare(strict_types=1);

namespace OCA\MemoriesFacetag\Migration;

use Closure;
use OCP\DB\ISchemaWrapper;
use OCP\DB\Types;
use OCP\Migration\IOutput;
use OCP\Migration\SimpleMigrationStep;

/**
 * The library re-scan: facetag_manual.source tells hand-placed tags ('manual') from faces the scan
 * added ('scan'), and facetag_scanned records which files the scan has been through.
 */
class Version000200Date20261126000000 extends SimpleMigrationStep {
	public function changeSchema(IOutput $output, Closure $schemaClosure, array $options): ?ISchemaWrapper {
		/** @var ISchemaWrapper $schema */
		$schema = $schemaClosure();
		$changed = false;

		$manual = $schema->getTable('facetag_manual');
		if (!$manual->hasColumn('source')) {
			$manual->addColumn('source', Types::STRING, ['notnull' => true, 'length' => 8, 'default' => 'manual']);
			$manual->addIndex(['source'], 'facetag_manual_src');
			$changed = true;
		}

		if (!$schema->hasTable('facetag_scanned')) {
			$table = $schema->createTable('facetag_scanned');
			$table->addColumn('file_id', Types::BIGINT, ['notnull' => true, 'unsigned' => true]);
			$table->addColumn('faces_found', Types::INTEGER, ['notnull' => true, 'default' => 0]);
			$table->addColumn('faces_added', Types::INTEGER, ['notnull' => true, 'default' => 0]);
			$table->addColumn('scanned_at', Types::BIGINT, ['notnull' => true, 'unsigned' => true]);
			$table->setPrimaryKey(['file_id']);
			$changed = true;
		}

		return $changed ? $schema : null;
	}
}
