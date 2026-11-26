<?php

declare(strict_types=1);

return [
	'routes' => [
		['name' => 'api#file', 'url' => '/api/file/{fileId}', 'verb' => 'GET'],
		['name' => 'api#locate', 'url' => '/api/file/{fileId}/locate', 'verb' => 'POST'],
		['name' => 'api#addTag', 'url' => '/api/file/{fileId}/tags', 'verb' => 'POST'],
		['name' => 'api#suggestForDetection', 'url' => '/api/detection/{id}/suggest', 'verb' => 'GET'],
		['name' => 'api#assign', 'url' => '/api/detection/{id}', 'verb' => 'PUT'],
		['name' => 'api#remove', 'url' => '/api/detection/{id}', 'verb' => 'DELETE'],
	],
];
