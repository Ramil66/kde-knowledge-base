CREATE TABLE raw_documents (
	id INTEGER NOT NULL, 
	document_type TEXT NOT NULL, 
	source_url TEXT, 
	content TEXT NOT NULL, 
	content_hash TEXT NOT NULL, 
	format TEXT NOT NULL, 
	created_at TEXT DEFAULT CURRENT_TIMESTAMP NOT NULL, 
	metadata JSON, 
	PRIMARY KEY (id)
);

CREATE UNIQUE INDEX uq_document_origin ON raw_documents (document_type, format, coalesce(source_url, ''), content_hash);

CREATE TABLE repositories (
	id INTEGER NOT NULL, 
	name TEXT NOT NULL, 
	url TEXT NOT NULL, 
	namespace TEXT, 
	description TEXT, 
	default_branch TEXT, 
	local_path TEXT, 
	architecture_summary TEXT, 
	build_instructions TEXT, 
	test_instructions TEXT, 
	created_at TEXT DEFAULT CURRENT_TIMESTAMP NOT NULL, 
	updated_at TEXT DEFAULT CURRENT_TIMESTAMP NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (url)
);

CREATE INDEX ix_repositories_name ON repositories (name);

CREATE TABLE tasks (
	id INTEGER NOT NULL, 
	title TEXT NOT NULL, 
	description TEXT, 
	external_id TEXT, 
	external_url TEXT, 
	source TEXT, 
	type TEXT, 
	status TEXT, 
	priority TEXT, 
	reproduction_steps TEXT, 
	actual_behavior TEXT, 
	expected_behavior TEXT, 
	solution_summary TEXT, 
	completed_at TEXT, 
	created_at TEXT DEFAULT CURRENT_TIMESTAMP NOT NULL, 
	updated_at TEXT DEFAULT CURRENT_TIMESTAMP NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (source, external_id), 
	CHECK (external_id IS NULL OR source IS NOT NULL), 
	CHECK (type IN ('bug','feature','refactoring','documentation','research','other'))
);

CREATE TABLE users (
	id INTEGER NOT NULL, 
	platform TEXT NOT NULL, 
	username TEXT NOT NULL, 
	display_name TEXT, 
	profile_url TEXT, 
	description TEXT, 
	PRIMARY KEY (id), 
	UNIQUE (platform, username)
);

CREATE TABLE branches (
	id INTEGER NOT NULL, 
	repository_id INTEGER NOT NULL, 
	name TEXT NOT NULL, 
	source_repository_url TEXT NOT NULL, 
	base_branch TEXT, 
	base_commit TEXT, 
	head_commit TEXT, 
	status TEXT, 
	solution_summary TEXT, 
	created_at TEXT DEFAULT CURRENT_TIMESTAMP NOT NULL, 
	updated_at TEXT DEFAULT CURRENT_TIMESTAMP NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (repository_id, source_repository_url, name), 
	FOREIGN KEY(repository_id) REFERENCES repositories (id) ON DELETE RESTRICT
);

CREATE INDEX ix_branches_repository_id ON branches (repository_id);

CREATE TABLE commits (
	id INTEGER NOT NULL, 
	repository_id INTEGER NOT NULL, 
	sha TEXT NOT NULL, 
	author_id INTEGER, 
	message TEXT, 
	authored_at TEXT, 
	committed_at TEXT, 
	url TEXT, 
	summary TEXT, 
	PRIMARY KEY (id), 
	UNIQUE (repository_id, sha), 
	FOREIGN KEY(repository_id) REFERENCES repositories (id) ON DELETE RESTRICT, 
	FOREIGN KEY(author_id) REFERENCES users (id) ON DELETE RESTRICT
);

CREATE INDEX ix_commits_author_id ON commits (author_id);

CREATE INDEX ix_commits_repository_id ON commits (repository_id);

CREATE TABLE repository_dependencies (
	entity_id INTEGER NOT NULL, 
	depends_on_id INTEGER NOT NULL, 
	PRIMARY KEY (entity_id, depends_on_id), 
	CHECK (entity_id <> depends_on_id), 
	FOREIGN KEY(entity_id) REFERENCES repositories (id) ON DELETE CASCADE, 
	FOREIGN KEY(depends_on_id) REFERENCES repositories (id) ON DELETE CASCADE
);

CREATE INDEX ix_repository_dependencies_depends_on_id ON repository_dependencies (depends_on_id);

CREATE INDEX ix_repository_dependencies_entity_id ON repository_dependencies (entity_id);

CREATE TABLE task_dependencies (
	entity_id INTEGER NOT NULL, 
	depends_on_id INTEGER NOT NULL, 
	PRIMARY KEY (entity_id, depends_on_id), 
	CHECK (entity_id <> depends_on_id), 
	FOREIGN KEY(entity_id) REFERENCES tasks (id) ON DELETE CASCADE, 
	FOREIGN KEY(depends_on_id) REFERENCES tasks (id) ON DELETE CASCADE
);

CREATE INDEX ix_task_dependencies_depends_on_id ON task_dependencies (depends_on_id);

CREATE INDEX ix_task_dependencies_entity_id ON task_dependencies (entity_id);

CREATE TABLE task_events (
	id INTEGER NOT NULL, 
	task_id INTEGER NOT NULL, 
	event_type TEXT NOT NULL, 
	description TEXT, 
	created_at TEXT, 
	PRIMARY KEY (id), 
	FOREIGN KEY(task_id) REFERENCES tasks (id) ON DELETE CASCADE
);

CREATE INDEX ix_task_events_task_id ON task_events (task_id);

CREATE TABLE changed_files (
	id INTEGER NOT NULL, 
	commit_id INTEGER NOT NULL, 
	old_path TEXT, 
	new_path TEXT, 
	change_type TEXT, 
	additions INTEGER, 
	deletions INTEGER, 
	diff TEXT, 
	PRIMARY KEY (id), 
	FOREIGN KEY(commit_id) REFERENCES commits (id) ON DELETE CASCADE
);

CREATE INDEX ix_changed_files_commit_id ON changed_files (commit_id);

CREATE TABLE merge_requests (
	id INTEGER NOT NULL, 
	branch_id INTEGER NOT NULL, 
	target_repository_id INTEGER NOT NULL, 
	author_id INTEGER, 
	external_id INTEGER NOT NULL, 
	title TEXT NOT NULL, 
	is_draft BOOLEAN, 
	url TEXT, 
	description TEXT, 
	source_branch TEXT, 
	target_branch TEXT, 
	status TEXT, 
	pipeline_status TEXT, 
	merged_at TEXT, 
	closed_at TEXT, 
	resolution_summary TEXT, 
	created_at TEXT DEFAULT CURRENT_TIMESTAMP NOT NULL, 
	updated_at TEXT DEFAULT CURRENT_TIMESTAMP NOT NULL, 
	PRIMARY KEY (id), 
	CHECK (external_id > 0), 
	UNIQUE (target_repository_id, external_id), 
	FOREIGN KEY(branch_id) REFERENCES branches (id) ON DELETE RESTRICT, 
	FOREIGN KEY(target_repository_id) REFERENCES repositories (id) ON DELETE RESTRICT, 
	FOREIGN KEY(author_id) REFERENCES users (id) ON DELETE RESTRICT
);

CREATE INDEX ix_merge_requests_author_id ON merge_requests (author_id);

CREATE INDEX ix_merge_requests_branch_id ON merge_requests (branch_id);

CREATE INDEX ix_merge_requests_target_repository_id ON merge_requests (target_repository_id);

CREATE TABLE task_branches (
	task_id INTEGER NOT NULL, 
	branch_id INTEGER NOT NULL, 
	role TEXT, 
	description TEXT, 
	PRIMARY KEY (task_id, branch_id), 
	FOREIGN KEY(task_id) REFERENCES tasks (id) ON DELETE CASCADE, 
	FOREIGN KEY(branch_id) REFERENCES branches (id) ON DELETE CASCADE
);

CREATE INDEX ix_task_branches_branch_id ON task_branches (branch_id);

CREATE INDEX ix_task_branches_task_id ON task_branches (task_id);

CREATE TABLE test_runs (
	id INTEGER NOT NULL, 
	branch_id INTEGER NOT NULL, 
	command TEXT, 
	revision TEXT, 
	result TEXT, 
	output TEXT, 
	created_at TEXT, 
	PRIMARY KEY (id), 
	FOREIGN KEY(branch_id) REFERENCES branches (id) ON DELETE CASCADE
);

CREATE INDEX ix_test_runs_branch_id ON test_runs (branch_id);

CREATE TABLE discussions (
	id INTEGER NOT NULL, 
	merge_request_id INTEGER NOT NULL, 
	external_id TEXT, 
	is_resolved BOOLEAN, 
	created_at TEXT, 
	resolved_at TEXT, 
	PRIMARY KEY (id), 
	UNIQUE (merge_request_id, external_id), 
	FOREIGN KEY(merge_request_id) REFERENCES merge_requests (id) ON DELETE CASCADE
);

CREATE INDEX ix_discussions_merge_request_id ON discussions (merge_request_id);

CREATE TABLE document_links (
	id INTEGER NOT NULL, 
	document_id INTEGER NOT NULL, 
	repository_id INTEGER, 
	task_id INTEGER, 
	merge_request_id INTEGER, 
	PRIMARY KEY (id), 
	CHECK ((repository_id IS NOT NULL) + (task_id IS NOT NULL) + (merge_request_id IS NOT NULL) = 1), 
	UNIQUE (document_id, repository_id), 
	UNIQUE (document_id, task_id), 
	UNIQUE (document_id, merge_request_id), 
	FOREIGN KEY(document_id) REFERENCES raw_documents (id) ON DELETE CASCADE, 
	FOREIGN KEY(repository_id) REFERENCES repositories (id) ON DELETE CASCADE, 
	FOREIGN KEY(task_id) REFERENCES tasks (id) ON DELETE CASCADE, 
	FOREIGN KEY(merge_request_id) REFERENCES merge_requests (id) ON DELETE CASCADE
);

CREATE INDEX ix_document_links_document_id ON document_links (document_id);

CREATE INDEX ix_document_links_merge_request_id ON document_links (merge_request_id);

CREATE INDEX ix_document_links_repository_id ON document_links (repository_id);

CREATE INDEX ix_document_links_task_id ON document_links (task_id);

CREATE TABLE mr_commits (
	merge_request_id INTEGER NOT NULL, 
	commit_id INTEGER NOT NULL, 
	PRIMARY KEY (merge_request_id, commit_id), 
	FOREIGN KEY(merge_request_id) REFERENCES merge_requests (id) ON DELETE CASCADE, 
	FOREIGN KEY(commit_id) REFERENCES commits (id) ON DELETE CASCADE
);

CREATE INDEX ix_mr_commits_commit_id ON mr_commits (commit_id);

CREATE INDEX ix_mr_commits_merge_request_id ON mr_commits (merge_request_id);

CREATE TABLE mr_dependencies (
	entity_id INTEGER NOT NULL, 
	depends_on_id INTEGER NOT NULL, 
	PRIMARY KEY (entity_id, depends_on_id), 
	CHECK (entity_id <> depends_on_id), 
	FOREIGN KEY(entity_id) REFERENCES merge_requests (id) ON DELETE CASCADE, 
	FOREIGN KEY(depends_on_id) REFERENCES merge_requests (id) ON DELETE CASCADE
);

CREATE INDEX ix_mr_dependencies_depends_on_id ON mr_dependencies (depends_on_id);

CREATE INDEX ix_mr_dependencies_entity_id ON mr_dependencies (entity_id);

CREATE TABLE comments (
	id INTEGER NOT NULL, 
	discussion_id INTEGER NOT NULL, 
	author_id INTEGER, 
	parent_comment_id INTEGER, 
	external_id TEXT, 
	body TEXT NOT NULL, 
	summary TEXT, 
	category TEXT, 
	url TEXT, 
	created_at TEXT, 
	updated_at TEXT, 
	PRIMARY KEY (id), 
	UNIQUE (discussion_id, external_id), 
	UNIQUE (discussion_id, id), 
	FOREIGN KEY(discussion_id, parent_comment_id) REFERENCES comments (discussion_id, id), 
	FOREIGN KEY(discussion_id) REFERENCES discussions (id) ON DELETE CASCADE, 
	FOREIGN KEY(author_id) REFERENCES users (id) ON DELETE RESTRICT
);

CREATE INDEX ix_comments_author_id ON comments (author_id);

CREATE INDEX ix_comments_discussion_id ON comments (discussion_id);

CREATE TABLE code_positions (
	id INTEGER NOT NULL, 
	comment_id INTEGER NOT NULL, 
	old_path TEXT, 
	new_path TEXT, 
	old_line INTEGER, 
	new_line INTEGER, 
	base_sha TEXT, 
	start_sha TEXT, 
	head_sha TEXT, 
	code_context TEXT, 
	PRIMARY KEY (id), 
	CHECK (new_path IS NOT NULL OR old_path IS NOT NULL), 
	CHECK (head_sha IS NOT NULL OR base_sha IS NOT NULL OR start_sha IS NOT NULL), 
	FOREIGN KEY(comment_id) REFERENCES comments (id) ON DELETE CASCADE
);

CREATE INDEX ix_code_positions_comment_id ON code_positions (comment_id);
