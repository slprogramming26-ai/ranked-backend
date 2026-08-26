-- TABELLE activities

CREATE TABLE activities (
	id SERIAL NOT NULL, 
	user_id INTEGER NOT NULL, 
	type VARCHAR NOT NULL, 
	payload INTEGER NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE
)



-- TABELLE blocks

CREATE TABLE blocks (
	blocker_id INTEGER NOT NULL, 
	blocked_id INTEGER NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (blocker_id, blocked_id), 
	FOREIGN KEY(blocker_id) REFERENCES users (id) ON DELETE CASCADE, 
	FOREIGN KEY(blocked_id) REFERENCES users (id) ON DELETE CASCADE
)



-- TABELLE comments

CREATE TABLE comments (
	id SERIAL NOT NULL, 
	user_id INTEGER, 
	post_id INTEGER, 
	comment VARCHAR NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE, 
	FOREIGN KEY(post_id) REFERENCES posts (id) ON DELETE CASCADE
)



-- TABELLE daily_bonus_log

CREATE TABLE daily_bonus_log (
	date DATE NOT NULL, 
	processed_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	awarded_count INTEGER DEFAULT 0 NOT NULL, 
	PRIMARY KEY (date)
)



-- TABELLE daily_targets

CREATE TABLE daily_targets (
	id SERIAL NOT NULL, 
	voter_id INTEGER NOT NULL, 
	target_user_id INTEGER NOT NULL, 
	date DATE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(voter_id) REFERENCES users (id) ON DELETE CASCADE, 
	FOREIGN KEY(target_user_id) REFERENCES users (id) ON DELETE CASCADE
)



-- TABELLE failed_image_deletions

CREATE TABLE failed_image_deletions (
	id SERIAL NOT NULL, 
	bucket VARCHAR NOT NULL, 
	s3_key VARCHAR NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id)
)



-- TABELLE feed_impressions

CREATE TABLE feed_impressions (
	id SERIAL NOT NULL, 
	user_id INTEGER NOT NULL, 
	post_id INTEGER NOT NULL, 
	feed_session_id VARCHAR NOT NULL, 
	feed_variant VARCHAR NOT NULL, 
	position INTEGER NOT NULL, 
	shown_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	dwell_ms INTEGER NOT NULL, 
	voted BOOLEAN DEFAULT false NOT NULL, 
	opened_comments BOOLEAN DEFAULT false NOT NULL, 
	shared BOOLEAN DEFAULT false NOT NULL, 
	reported BOOLEAN DEFAULT false NOT NULL, 
	features JSONB, 
	feature_version INTEGER, 
	received_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE, 
	FOREIGN KEY(post_id) REFERENCES posts (id) ON DELETE CASCADE
)



-- TABELLE follows

CREATE TABLE follows (
	follower_id INTEGER NOT NULL, 
	followee_id INTEGER NOT NULL, 
	PRIMARY KEY (follower_id, followee_id), 
	FOREIGN KEY(follower_id) REFERENCES users (id) ON DELETE CASCADE, 
	FOREIGN KEY(followee_id) REFERENCES users (id) ON DELETE CASCADE
)



-- TABELLE group_chat_epochs

CREATE TABLE group_chat_epochs (
	group_chat_id INTEGER NOT NULL, 
	key_version INTEGER NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (group_chat_id, key_version), 
	FOREIGN KEY(group_chat_id) REFERENCES group_chats (group_chat_id) ON DELETE CASCADE
)



-- TABELLE group_chat_keys

CREATE TABLE group_chat_keys (
	id SERIAL NOT NULL, 
	group_chat_id INTEGER NOT NULL, 
	key_version INTEGER NOT NULL, 
	recipient_id INTEGER NOT NULL, 
	encrypted_key VARCHAR NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_group_key_version_recipient UNIQUE (group_chat_id, key_version, recipient_id), 
	FOREIGN KEY(group_chat_id, key_version) REFERENCES group_chat_epochs (group_chat_id, key_version) ON DELETE CASCADE, 
	FOREIGN KEY(group_chat_id) REFERENCES group_chats (group_chat_id) ON DELETE CASCADE, 
	FOREIGN KEY(recipient_id) REFERENCES users (id) ON DELETE CASCADE
)



-- TABELLE group_chat_memberships

CREATE TABLE group_chat_memberships (
	group_chat_id INTEGER NOT NULL, 
	participant_id INTEGER NOT NULL, 
	joined_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (group_chat_id, participant_id), 
	FOREIGN KEY(group_chat_id) REFERENCES group_chats (group_chat_id) ON DELETE CASCADE, 
	FOREIGN KEY(participant_id) REFERENCES users (id) ON DELETE CASCADE
)



-- TABELLE group_chats

CREATE TABLE group_chats (
	group_chat_id SERIAL NOT NULL, 
	creator_id INTEGER NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	group_name VARCHAR, 
	profile_picture VARCHAR, 
	needs_rekey BOOLEAN DEFAULT 'False' NOT NULL, 
	PRIMARY KEY (group_chat_id), 
	FOREIGN KEY(creator_id) REFERENCES users (id) ON DELETE CASCADE
)



-- TABELLE group_chats_join_codes

CREATE TABLE group_chats_join_codes (
	id SERIAL NOT NULL, 
	code INTEGER NOT NULL, 
	group_chat_id INTEGER NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (code), 
	FOREIGN KEY(group_chat_id) REFERENCES group_chats (group_chat_id) ON DELETE CASCADE
)



-- TABELLE group_message

CREATE TABLE group_message (
	id SERIAL NOT NULL, 
	group_chat_id INTEGER NOT NULL, 
	sender_id INTEGER NOT NULL, 
	message VARCHAR NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	client_msg_id VARCHAR, 
	key_version INTEGER, 
	PRIMARY KEY (id), 
	FOREIGN KEY(group_chat_id, key_version) REFERENCES group_chat_epochs (group_chat_id, key_version) ON DELETE CASCADE, 
	FOREIGN KEY(group_chat_id) REFERENCES group_chats (group_chat_id) ON DELETE CASCADE, 
	FOREIGN KEY(sender_id) REFERENCES users (id) ON DELETE CASCADE
)



-- TABELLE locations

CREATE TABLE locations (
	id SERIAL NOT NULL, 
	name VARCHAR NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (name)
)



-- TABELLE message

CREATE TABLE message (
	id SERIAL NOT NULL, 
	sender_id INTEGER NOT NULL, 
	recipient_id INTEGER NOT NULL, 
	message VARCHAR NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	client_msg_id VARCHAR, 
	PRIMARY KEY (id), 
	FOREIGN KEY(sender_id) REFERENCES users (id) ON DELETE CASCADE, 
	FOREIGN KEY(recipient_id) REFERENCES users (id) ON DELETE CASCADE
)



-- TABELLE posts

CREATE TABLE posts (
	id SERIAL NOT NULL, 
	title VARCHAR NOT NULL, 
	content VARCHAR NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	owner_id INTEGER NOT NULL, 
	image_url VARCHAR, 
	flag VARCHAR, 
	location_id INTEGER, 
	vote_count INTEGER DEFAULT 0 NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(owner_id) REFERENCES users (id) ON DELETE CASCADE, 
	FOREIGN KEY(location_id) REFERENCES locations (id) ON DELETE SET NULL
)



-- TABELLE ranking_scores

CREATE TABLE ranking_scores (
	id SERIAL NOT NULL, 
	voter_id INTEGER NOT NULL, 
	post_id INTEGER NOT NULL, 
	direction BOOLEAN NOT NULL, 
	points INTEGER NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(voter_id) REFERENCES users (id) ON DELETE CASCADE, 
	FOREIGN KEY(post_id) REFERENCES posts (id) ON DELETE CASCADE
)



-- TABELLE refresh_tokens

CREATE TABLE refresh_tokens (
	id SERIAL NOT NULL, 
	user_id INTEGER NOT NULL, 
	token_hash VARCHAR NOT NULL, 
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE, 
	UNIQUE (token_hash)
)



-- TABELLE reports

CREATE TABLE reports (
	id SERIAL NOT NULL, 
	reporter_id INTEGER NOT NULL, 
	reported_user_id INTEGER NOT NULL, 
	post_id INTEGER, 
	story_id INTEGER, 
	comment_id INTEGER, 
	reason VARCHAR NOT NULL, 
	status VARCHAR DEFAULT 'pending' NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(reporter_id) REFERENCES users (id) ON DELETE CASCADE, 
	FOREIGN KEY(reported_user_id) REFERENCES users (id) ON DELETE CASCADE, 
	FOREIGN KEY(post_id) REFERENCES posts (id) ON DELETE CASCADE, 
	FOREIGN KEY(story_id) REFERENCES stories (id) ON DELETE CASCADE, 
	FOREIGN KEY(comment_id) REFERENCES comments (id) ON DELETE CASCADE
)



-- TABELLE stories

CREATE TABLE stories (
	id SERIAL NOT NULL, 
	owner_id INTEGER NOT NULL, 
	image_url VARCHAR NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(owner_id) REFERENCES users (id) ON DELETE CASCADE
)



-- TABELLE user_keys

CREATE TABLE user_keys (
	user_id INTEGER NOT NULL, 
	public_key VARCHAR NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (user_id), 
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE
)



-- TABELLE users

CREATE TABLE users (
	id SERIAL NOT NULL, 
	email VARCHAR NOT NULL, 
	passwort VARCHAR NOT NULL, 
	username VARCHAR NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	vibe_factor_1 VARCHAR, 
	vibe_factor_2 VARCHAR, 
	biography VARCHAR, 
	profile_picture_url VARCHAR, 
	ranking_enabled BOOLEAN DEFAULT 'False' NOT NULL, 
	xp INTEGER DEFAULT 0 NOT NULL, 
	streak_count INTEGER DEFAULT 0 NOT NULL, 
	last_swipe_date DATE, 
	location_id INTEGER, 
	PRIMARY KEY (id), 
	UNIQUE (email), 
	UNIQUE (username), 
	FOREIGN KEY(location_id) REFERENCES locations (id) ON DELETE SET NULL
)



-- TABELLE votes

CREATE TABLE votes (
	user_id INTEGER NOT NULL, 
	post_id INTEGER NOT NULL, 
	PRIMARY KEY (user_id, post_id), 
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE, 
	FOREIGN KEY(post_id) REFERENCES posts (id) ON DELETE CASCADE
)



-- QUERY feed
SELECT posts.id, posts.title, posts.content, posts.created_at, posts.owner_id, posts.image_url, posts.flag, posts.location_id, posts.vote_count 
FROM posts 
WHERE (posts.title LIKE '%%' || %(title_1)s || '%%') AND (posts.id NOT IN (__[POSTCOMPILE_id_1])) AND posts.created_at >= %(created_at_1)s ORDER BY (%(vote_count_1)s * posts.vote_count - %(param_1)s * (EXTRACT(epoch FROM now() - posts.created_at) / CAST(%(param_2)s AS NUMERIC))) + CASE WHEN (EXISTS (SELECT 1 
FROM follows 
WHERE follows.follower_id = %(follower_id_1)s AND follows.followee_id = posts.owner_id)) THEN %(param_3)s ELSE %(param_4)s END + CASE WHEN (EXISTS (SELECT 1 
FROM users 
WHERE users.id = posts.owner_id AND (users.vibe_factor_1 IN (__[POSTCOMPILE_vibe_factor_1_1]) OR users.vibe_factor_2 IN (__[POSTCOMPILE_vibe_factor_2_1])))) THEN %(param_5)s ELSE %(param_6)s END DESC, posts.created_at DESC 
 LIMIT %(param_7)s OFFSET %(param_8)s

-- STATEMENT impression_upsert
INSERT INTO feed_impressions (user_id, post_id, feed_session_id, dwell_ms) VALUES (%(user_id_m0)s, %(post_id_m0)s, %(feed_session_id_m0)s, %(dwell_ms_m0)s) ON CONFLICT (user_id, feed_session_id, post_id) DO UPDATE SET dwell_ms = greatest(feed_impressions.dwell_ms, excluded.dwell_ms), voted = (feed_impressions.voted OR excluded.voted), opened_comments = (feed_impressions.opened_comments OR excluded.opened_comments), shared = (feed_impressions.shared OR excluded.shared), reported = (feed_impressions.reported OR excluded.reported)

-- QUERY blocked_user_ids
SELECT blocks.blocker_id, blocks.blocked_id, blocks.created_at 
FROM blocks 
WHERE blocks.blocker_id = %(blocker_id_1)s OR blocks.blocked_id = %(blocked_id_1)s

-- QUERY aktuelle_epoche
SELECT max(group_chat_epochs.key_version) AS max_1 
FROM group_chat_epochs 
WHERE group_chat_epochs.group_chat_id = %(group_chat_id_1)s

-- QUERY duplikat_lookup
SELECT message.id, message.sender_id, message.recipient_id, message.message, message.created_at, message.client_msg_id 
FROM message 
WHERE message.sender_id = %(sender_id_1)s AND message.client_msg_id = %(client_msg_id_1)s
