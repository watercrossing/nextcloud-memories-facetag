<?php

declare(strict_types=1);

namespace OCA\MemoriesFacetag\Migration;

use Closure;
use OCP\DB\ISchemaWrapper;
use OCP\DB\Types;
use OCP\Migration\IOutput;
use OCP\Migration\SimpleMigrationStep;

/**
 * Remembers which rows of recognize_face_detections were placed by hand,
 * so they can be shown as such and deleted outright rather than unassigned.
 */
class Version000100Date20261125000000 extends SimpleMigrationStep {
	public function changeSchema(IOutput $output, Closure $schemaClosure, array $options): ?ISchemaWrapper {
		/** @var ISchemaWrapper $schema */
		$schema = $schemaClosure();
		if ($schema->hasTable('facetag_manual')) {
			return null;
		}
		$table = $schema->createTable('facetag_manual');
		$table->addColumn('detection_id', Types::BIGINT, ['notnull' => true, 'unsigned' => true]);
		$table->addColumn('user_id', Types::STRING, ['notnull' => true, 'length' => 64]);
		$table->addColumn('file_id', Types::BIGINT, ['notnull' => true, 'unsigned' => true]);
		$table->addColumn('found_face', Types::BOOLEAN, ['notnull' => false, 'default' => false]);
		$table->addColumn('created_at', Types::BIGINT, ['notnull' => true, 'unsigned' => true]);
		$table->setPrimaryKey(['detection_id']);
		$table->addIndex(['user_id', 'file_id'], 'facetag_manual_uf');
		return $schema;
	}
}
