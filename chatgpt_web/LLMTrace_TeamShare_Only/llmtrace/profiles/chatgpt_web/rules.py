"""
LLMTrace - ChatGPT Web rules.py (DRAFT)

Purpose
-------
Declarative rule draft derived from the A01~A13 ChatGPT Web experiments.

Important design rules
----------------------
1. This file does NOT parse Chrome artifacts. Parsers produce Observations.
2. This file does NOT change the mandatory output schemas.
3. A rule may create an Inference only when the evidence described here is present.
4. Useful facts that do not safely identify a user action stay in CONTEXT_RULES.
5. No numeric confidence scores are used.
6. BEFORE/AFTER diff is ground truth for validation, not a runtime requirement.
7. A single acquired state may still contain previous/residual records; STATE_TRANSITION_RULES
   may use those when the parser can distinguish previous/residual from current state.

This is a rule/schema draft. The inference engine is expected to implement the matcher,
association, confidence, suppression and state-transition semantics described below.
"""

from __future__ import annotations

from typing import Any, Final


PROFILE: Final[str] = "chatgpt-web"
RULESET_VERSION: Final[str] = "0.1-draft-a01-a13"

CONFIDENCE_VALUES: Final[tuple[str, ...]] = (
    "confirmed",
    "suspected",
    "presumed",
    "undecided",
)


# ---------------------------------------------------------------------------
# 0. Engine assumptions
# ---------------------------------------------------------------------------
#
# The matcher examples below assume a normalized Observation exposes, directly
# or through a data/payload mapping, fields such as:
#
#   artifact
#   fact_type
#   ts
#   url
#   scheme
#   hostname
#   path
#   query
#   fragment_route
#   fragment_query
#   conversation_id
#   message_id
#   file_id
#   account_id
#   state
#   auth_session_logging_id
#
# Exact Observation storage is defined by core/schema.py, not by this file.
#
# Generic matcher operators expected by the engine:
#
#   eq, neq, in, exists, regex, prefix, suffix, contains
#   query_eq, fragment_query_eq
#
# Generic association policies expected by the engine:
#
#   same_conversation_id
#   same_message_id
#   same_file_id
#   same_account_id
#   same_oauth_state
#   same_auth_session_logging_id
#   history_from_visit
#   same_download_url
#   ordered_after
#
# "required": False means the evidence is optional and its absence must not
# fabricate a timestamp or fail a rule whose minimum evidence is otherwise met.


# ---------------------------------------------------------------------------
# 1. Timeline display filters
# ---------------------------------------------------------------------------

TIMELINE_FILTERS: Final[list[dict[str, Any]]] = [
    {
        "filter_id": "TL-HISTORY-CHATGPT-01",
        "artifact": "history",
        "match": {
            "hostname": {"in": ["chatgpt.com", "auth.openai.com"]},
        },
        "note": (
            "Main timeline displays ChatGPT/OpenAI History observations. "
            "Third-party OAuth pages may still be rule evidence without becoming main pins."
        ),
    },
    {
        "filter_id": "TL-CACHE-CHATGPT-01",
        "artifact": "cache",
        "match": {
            "hostname": {"in": ["chatgpt.com", "auth.openai.com"]},
        },
        "note": (
            "Cache pins must represent validated request/resource observations, "
            "not arbitrary strings containing chatgpt.com."
        ),
    },
]


# ---------------------------------------------------------------------------
# 2. Association policies
# ---------------------------------------------------------------------------

ASSOCIATION_POLICIES: Final[dict[str, dict[str, Any]]] = {
    "same_conversation_id": {
        "fields": ["conversation_id"],
        "strength": "strong",
    },
    "same_message_id": {
        "fields": ["message_id"],
        "strength": "strong",
    },
    "same_file_id": {
        "fields": ["file_id"],
        "strength": "strong",
    },
    "same_account_id": {
        "fields": ["account_id"],
        "strength": "strong",
    },
    "same_oauth_state": {
        # URL-derived Observations keep query parameters under ``query``.
        # The legacy top-level name remains as a fallback for old imported
        # Observation JSONL files.
        "fields": ["query.state", "oauth_state"],
        "strength": "strong",
    },
    "same_auth_session_logging_id": {
        "fields": [
            "query.auth_session_logging_id",
            "auth_session_logging_id",
        ],
        "strength": "strong",
    },
    "history_from_visit": {
        "fields": ["visit_id", "from_visit"],
        "strength": "strong",
        "relation": "right.from_visit == left.visit_id",
    },
    "same_download_url": {
        "fields": ["url"],
        "strength": "strong",
        "note": "Prefer exact normalized URL or extracted file_id over loose filename matching.",
    },
    "ordered_after": {
        "fields": ["ts"],
        "strength": "weak",
        "note": (
            "Temporal ordering is a fallback only. Do not attach ambiguous evidence "
            "to the nearest inference solely because timestamps are close."
        ),
    },
    "conversation_prompt_edit_transition": {
        "fields": ["message_sequence", "current_node"],
        "strength": "strong",
        "note": (
            "The active branches must share a prefix and then replace a user "
            "message with a different user-message branch followed by an assistant response."
        ),
    },
    "memory_source_checksum_changed": {
        "fields": ["source_checksum"],
        "strength": "strong",
        "note": (
            "Two memory-summary snapshots must expose different non-empty source checksums."
        ),
    },
}


# Each History-anchored rule is evaluated inside the range that starts at its
# matching History observation and ends at the next History observation that
# matches the same behavior anchor.  A missing next anchor leaves the range
# open, because the final-window closing policy is intentionally unresolved.
HISTORY_WINDOW_POLICY: Final[dict[str, Any]] = {
    "enabled": True,
    "mode": "next_matching_history",
    "last_window": "open",
}


# ---------------------------------------------------------------------------
# 3. Shared match fragments
# ---------------------------------------------------------------------------

MATCH_CHATGPT_HISTORY: Final[dict[str, Any]] = {
    "artifact": "history",
    "hostname": {"eq": "chatgpt.com"},
}

MATCH_CHATGPT_CACHE: Final[dict[str, Any]] = {
    "artifact": "cache",
    "hostname": {"eq": "chatgpt.com"},
}

MATCH_CANONICAL_CONVERSATION_PATH: Final[dict[str, Any]] = {
    "path": {"regex": r"^/c/(?!WEB:)[^/?#]+$"},
}

MATCH_WEB_TEMP_CONVERSATION_PATH: Final[dict[str, Any]] = {
    "path": {"regex": r"^/c/WEB:[^/?#]+$"},
}


# ---------------------------------------------------------------------------
# 4. Behavior inference rules
# ---------------------------------------------------------------------------

INFERENCE_RULES: Final[list[dict[str, Any]]] = [

    # -----------------------------------------------------------------------
    # A01 - ChatGPT Web access
    # -----------------------------------------------------------------------
    {
        "rule_id": "R-ACCESS-01",
        "behavior_id": "B01",
        "behavior_name": "ChatGPT Web 접근",
        "mode": "event_range",

        "start": {
            "ref": "access_history",
            "artifact": "history",
            "fact_type": "url_visit",
            "match": {
                "hostname": {"eq": "chatgpt.com"},
                "path": {"eq": "/"},
            },
            "required": True,
        },

        "end": {
            "ref": "unauth_cache",
            "artifact": "cache",
            "fact_type": "http_cache_entry",
            "match": {
                "hostname": {"eq": "chatgpt.com"},
                "path": {"prefix": "/unauth-mweb/"},
            },
            "required": False,
            "select": "last_after_start",
        },

        "confidence": [
            {
                "level": "confirmed",
                "when": {"all_refs": ["access_history"]},
            },
        ],

        "notes": [
            "History is the semantic Start anchor even if some Cache entries predate it.",
            "typed_count alone is not treated as navigation method proof; visits.transition is preferred.",
            "Optional Cache End must not fabricate when_to when absent.",
        ],
    },

    # -----------------------------------------------------------------------
    # A02 - Login
    # -----------------------------------------------------------------------
    {
        "rule_id": "R-LOGIN-01",
        "behavior_id": "B02",
        "behavior_name": "ChatGPT 로그인",
        "mode": "event_range",

        "start": {
            "ref": "login_start",
            "artifact": "history",
            "fact_type": "url_visit",
            "match": {
                "hostname": {"eq": "chatgpt.com"},
                "path": {"eq": "/auth/login_with"},
            },
            "extract": [
                "query.auth_session_logging_id",
                "query.connection",
                "query.login_hint",
            ],
            "required": True,
        },

        "milestones": [
            {
                "ref": "authorize",
                "artifact": "history",
                "fact_type": "url_visit",
                "match": {
                    "hostname": {"eq": "auth.openai.com"},
                    "path": {"eq": "/api/accounts/authorize"},
                },
                "extract": ["query.auth_session_logging_id", "query.state"],
                "required": True,
            },
            {
                "ref": "login_callback",
                "artifact": "history",
                "fact_type": "url_visit",
                "match": {
                    "hostname": {"eq": "chatgpt.com"},
                    "path": {"eq": "/api/auth/callback/openai"},
                    "query.code": {"exists": True},
                    "query.state": {"exists": True},
                },
                "extract": ["query.state"],
                "required": True,
            },
        ],

        "end": {
            "ref": "me_after_login",
            "artifact": "cache",
            "fact_type_in": ["http_cache_entry", "account_observation"],
            "match": {
                "hostname": {"eq": "chatgpt.com"},
                "path": {"eq": "/backend-api/me"},
            },
            "required": False,
            "select": "first_after_ref:login_callback",
        },

        "association": [
            {
                "policy": "same_auth_session_logging_id",
                "left": "login_start",
                "right": "authorize",
                "required": True,
            },
            {
                "policy": "same_oauth_state",
                "left": "authorize",
                "right": "login_callback",
                "required": True,
            },
        ],

        "confidence": [
            {
                "level": "confirmed",
                "when": {"all_refs": ["login_start", "authorize", "login_callback"]},
            },
        ],

        "notes": [
            "A failed OAuth attempt without a matching state callback must not produce successful login.",
            "The callback path differentiates ChatGPT login from Connector OAuth.",
        ],
    },

    # -----------------------------------------------------------------------
    # A03 - Logout
    # -----------------------------------------------------------------------
    {
        "rule_id": "R-LOGOUT-01",
        "behavior_id": "B03",
        "behavior_name": "ChatGPT 로그아웃",
        "mode": "event_range",

        "start": {
            "ref": "logout_history",
            "artifact": "history",
            "fact_type": "url_visit",
            "match": {
                "hostname": {"eq": "chatgpt.com"},
                "path": {"eq": "/auth/logout"},
            },
            "required": True,
        },

        "milestones": [
            {
                "ref": "return_home",
                "artifact": "history",
                "fact_type": "url_visit",
                "match": {
                    "hostname": {"eq": "chatgpt.com"},
                    "path": {"eq": "/"},
                },
                "required": False,
            },
            {
                "ref": "signout_state",
                "artifact": "local_storage",
                "fact_type": "kv_record",
                "match": {
                    "key": {"eq": "nextauth.message"},
                    "json.event": {"eq": "session"},
                    "json.data.trigger": {"eq": "signout"},
                },
                "required": False,
            },
            {
                "ref": "logout_marker",
                "artifact": "cache",
                "fact_type": "http_cache_entry",
                "match": {
                    "hostname": {"eq": "chatgpt.com"},
                    "path": {"eq": "/auth/logout"},
                },
                "extract": ["logout_reason", "logout_source"],
                "required": False,
            },
        ],

        "end": {
            "ref": "me_after_logout",
            "artifact": "cache",
            "fact_type_in": ["http_cache_entry", "account_observation"],
            "match": {
                "hostname": {"eq": "chatgpt.com"},
                "path": {"eq": "/backend-api/me"},
            },
            "required": False,
            "select": "first_after_start",
        },

        "exclude": [
            {
                "ref": "logout_marker",
                "when": {
                    "any": [
                        {"field": "logout_reason", "eq": "confirm_delete_account"},
                        {
                            "field": "logout_source",
                            "eq": "settings.confirm-delete-account-modal",
                        },
                    ]
                },
                "reason": "Account-delete flow is handled by R-ACCOUNT-DELETE-FLOW-01.",
            }
        ],

        "confidence": [
            {
                "level": "confirmed",
                "when": {
                    "all_refs": ["logout_history"],
                    "any_refs": ["return_home", "signout_state", "logout_marker"],
                },
            },
            {
                "level": "suspected",
                "when": {"all_refs": ["logout_history"]},
            },
        ],

        "notes": [
            "Do not use session-token absence as logout proof.",
            "logout_reason=logout_confirm_modal with logout_source=modal.logout-confirm-modal identifies ordinary logout confirmation.",
            "logout_reason=confirm_delete_account with logout_source=settings.confirm-delete-account-modal identifies the separate account-delete flow.",
        ],
    },

    # -----------------------------------------------------------------------
    # A04 - Account switch flow
    # -----------------------------------------------------------------------
    {
        "rule_id": "R-ACCOUNT-SWITCH-01",
        "behavior_id": "B04",
        "behavior_name": "ChatGPT 계정 전환 흐름",
        "mode": "event_range",

        "start": {
            "ref": "switch_history",
            "artifact": "history",
            "fact_type": "url_visit",
            "match": {
                "hostname": {"eq": "chatgpt.com"},
                "path": {"eq": "/"},
                "query.account_switch": {"eq": "true"},
            },
            "required": True,
        },

        "milestones": [
            {
                "ref": "switch_session",
                "artifact": "sessions",
                "fact_type": "navigation_record",
                "match": {
                    "original_request_url.query.account_switch": {"eq": "true"},
                },
                "required": False,
            },
        ],

        "end": {
            "ref": "me_after_switch",
            "artifact": "cache",
            "fact_type": "account_observation",
            "match": {
                "hostname": {"eq": "chatgpt.com"},
                "path": {"eq": "/backend-api/me"},
            },
            "required": False,
            "select": "first_after_start",
        },

        "exclude": [
            {
                "ref": "switch_history",
                "when": {
                    "field": "query.account_switch_login",
                    "eq": "add_account",
                },
                "reason": "Adding an account is not the same as switching accounts.",
            }
        ],

        "confidence": [
            {
                "level": "confirmed",
                "when": {
                    "all_refs": ["switch_history"],
                    "any_refs": ["switch_session", "me_after_switch"],
                },
            },
            {
                "level": "suspected",
                "when": {"all_refs": ["switch_history"]},
            },
        ],

        "notes": [
            "A single post-switch /me observation identifies the account observed after the flow, not A→B.",
            "Only claim A→B when both pre/post account observations are actually present and associated.",
        ],
    },

    # -----------------------------------------------------------------------
    # A05 - New conversation creation
    # -----------------------------------------------------------------------
    {
        "rule_id": "R-CONVERSATION-CREATE-01",
        "behavior_id": "B05",
        "behavior_name": "새 대화 생성",
        "mode": "event_range",

        "start": {
            "ref": "web_route",
            "artifact": "history",
            "fact_type": "conversation_visit",
            "match": {
                "hostname": {"eq": "chatgpt.com"},
                "path": {"regex": r"^/c/WEB:[^/?#]+$"},
            },
            "extract": ["web_temporary_id"],
            "required": True,
        },

        "milestones": [
            {
                "ref": "create_stream",
                "artifact": "cache",
                "fact_type": "conversation_stream_status",
                "match": {
                    "hostname": {"eq": "chatgpt.com"},
                    "path": {
                        "regex": (
                            r"^/backend-api/conversation/"
                            r"(?P<conversation_id>[^/]+)/stream_status$"
                        )
                    },
                    "json.status": {"eq": "IS_STREAMING"},
                },
                "extract": ["conversation_id"],
                "required": False,
            },
            {
                "ref": "canonical_history",
                "artifact": "history",
                "fact_type": "conversation_visit",
                "match": {
                    "hostname": {"eq": "chatgpt.com"},
                    "path": {"regex": r"^/c/(?P<conversation_id>(?!WEB:)[^/?#]+)$"},
                },
                "extract": ["conversation_id"],
                "required": False,
            },
            {
                "ref": "null_thread_draft",
                "artifact": "local_storage",
                "fact_type": "conversation_draft",
                "match": {
                    "draft_id": {"eq": "null_thread"},
                },
                "required": False,
            },
        ],

        "association": [
            {
                "policy": "same_conversation_id",
                "left": "create_stream",
                "right": "canonical_history",
                "required": False,
                "note": "WEB temporary ID must NOT be compared to the real conversation_id.",
            }
        ],

        "confidence": [
            {
                "level": "confirmed",
                "when": {
                    "all_refs": ["web_route", "create_stream", "canonical_history"],
                    "relations": [
                        "create_stream.conversation_id == canonical_history.conversation_id"
                    ],
                },
            },
            {
                "level": "suspected",
                "when": {"all_refs": ["web_route"]},
            },
        ],

        "notes": [
            "WEB:<temporary-id> and real conversation_id are different namespaces.",
            "null_thread draft and New chat→title residual are supporting only.",
        ],
    },

    # -----------------------------------------------------------------------
    # A07 - Prompt edit and resend
    # -----------------------------------------------------------------------
    {
        "rule_id": "R-PROMPT-EDIT-01",
        "behavior_id": "B07",
        "behavior_name": "프롬프트 수정 및 재전송",
        "mode": "event_range",

        "start": {
            "ref": "before_snapshot",
            "artifact": "cache",
            "fact_type": "conversation_snapshot",
            "match": {
                "conversation_id": {"exists": True},
                "message_sequence": {"exists": True},
            },
            "extract": ["conversation_id", "current_node", "branch_signature"],
            "required": True,
        },

        "milestones": [
            {
                "ref": "edit_stream",
                "artifact": "cache",
                "fact_type": "conversation_stream_status",
                "match": {
                    "json.status": {"in": ["IS_STREAMING", "COMPLETE"]},
                },
                "extract": ["conversation_id"],
                "required": False,
                "select": "first_after_ref:before_snapshot",
            },
        ],

        "end": {
            "ref": "after_snapshot",
            "artifact": "cache",
            "fact_type": "conversation_snapshot",
            "match": {
                "conversation_id": {"exists": True},
                "message_sequence": {"exists": True},
            },
            "extract": ["conversation_id", "current_node", "branch_signature"],
            "required": True,
            "select": "next_after_ref:before_snapshot",
        },

        "association": [
            {
                "policy": "same_conversation_id",
                "left": "before_snapshot",
                "right": "after_snapshot",
                "required": True,
            },
            {
                "policy": "conversation_prompt_edit_transition",
                "left": "before_snapshot",
                "right": "after_snapshot",
                "required": True,
            },
            {
                "policy": "same_conversation_id",
                "left": "before_snapshot",
                "right": "edit_stream",
                "required": False,
            },
            {
                "policy": "ordered_after",
                "left": "edit_stream",
                "right": "after_snapshot",
                "required": False,
            },
        ],

        "confidence": [
            {
                "level": "confirmed",
                "when": {"all_refs": ["before_snapshot", "edit_stream", "after_snapshot"]},
            },
            {
                "level": "presumed",
                "when": {"all_refs": ["before_snapshot", "after_snapshot"]},
            },
        ],

        "notes": [
            "A single current conversation snapshot cannot prove that a prompt was edited.",
            "An append-only user/assistant turn is excluded; the active message branch must diverge at a user message.",
            "A streaming/completion status strengthens timing but does not identify prompt edit by itself.",
        ],
    },

    # -----------------------------------------------------------------------
    # A08 - Memory / Personalization management
    # -----------------------------------------------------------------------
    {
        "rule_id": "R-MEMORY-MANAGEMENT-OPEN-01",
        "behavior_id": "B08",
        "behavior_name": "Memory / Personalization 관리 활동",
        "mode": "event_range",

        "start": {
            "ref": "memory_settings",
            "artifact": "history",
            "fact_type": "url_visit",
            "match": {
                "hostname": {"eq": "chatgpt.com"},
                "fragment_route": {
                    "regex": r"^settings/Personalization(?:$|/)"
                },
            },
            "required": True,
        },

        "milestones": [
            {
                "ref": "memory_section",
                "artifact": "history",
                "fact_type": "url_visit",
                "match": {
                    "hostname": {"eq": "chatgpt.com"},
                    "fragment_route": {
                        "regex": r"^settings/Personalization(?:$|/)"
                    },
                    "fragment_query.section": {"eq": "memory"},
                },
                "required": False,
            },
            {
                "ref": "memory_api",
                "artifact": "cache",
                "fact_type_in": [
                    "http_cache_entry",
                    "memory_list_snapshot",
                    "memory_summary_checksum",
                    "memory_correction_capability",
                ],
                "match": {
                    "hostname": {"eq": "chatgpt.com"},
                    "path": {
                        "regex": (
                            r"^/backend-api/memories"
                            r"(?:$|\?|/about_you/)"
                        )
                    },
                },
                "required": False,
            },
        ],

        "confidence": [
            {
                "level": "confirmed",
                "when": {
                    "all_refs": ["memory_settings"],
                    "any_refs": ["memory_section", "memory_api"],
                },
            },
            {
                "level": "suspected",
                "when": {"all_refs": ["memory_settings"]},
            },
        ],

        "notes": [
            "section=memory is inside the URL fragment, not the normal URL query.",
            "Do not infer memory edit/delete from selection_correction_actions or checksum alone.",
        ],
    },

    {
        "rule_id": "R-MEMORY-STATE-CHANGE-01",
        "behavior_id": "B08",
        "behavior_name": "Memory / Personalization 정보 상태 변경",
        "mode": "event_range",

        "start": {
            "ref": "before_memory_checksum",
            "artifact": "cache",
            "fact_type": "memory_summary_checksum",
            "match": {
                "source_checksum": {"exists": True},
            },
            "extract": [
                "source_checksum", "cached_source_checksum",
                "cached_generated_at", "is_stale",
            ],
            "required": True,
        },

        "end": {
            "ref": "after_memory_checksum",
            "artifact": "cache",
            "fact_type": "memory_summary_checksum",
            "match": {
                "source_checksum": {"exists": True},
            },
            "extract": [
                "source_checksum", "cached_source_checksum",
                "cached_generated_at", "is_stale",
            ],
            "required": True,
            "select": "next_after_ref:before_memory_checksum",
        },

        "association": [
            {
                "policy": "memory_source_checksum_changed",
                "left": "before_memory_checksum",
                "right": "after_memory_checksum",
                "required": True,
            },
        ],

        "confidence": [
            {
                "level": "confirmed",
                "when": {
                    "all": [
                        {"all_refs": ["before_memory_checksum", "after_memory_checksum"]},
                        {"field": "after_memory_checksum.is_stale", "eq": False},
                    ],
                    "relations": [
                        "after_memory_checksum.cached_source_checksum == after_memory_checksum.source_checksum",
                        "before_memory_checksum.cached_source_checksum != after_memory_checksum.cached_source_checksum",
                    ],
                },
            },
            {
                "level": "presumed",
                "when": {"all_refs": ["before_memory_checksum", "after_memory_checksum"]},
            },
        ],

        "alternatives": [
            "프롬프트를 통한 메모리 요청",
            "Personalization 화면의 직접 수정",
            "서비스의 자동 요약 재생성",
        ],

        "notes": [
            "This confirms a change in the memory/About You source state, not the exact user interaction that caused it.",
            "A changed checksum alone must not be labeled as direct edit or deletion.",
            "cached_generated_at is summary generation metadata; Cache response_time remains the observation boundary.",
        ],
    },

    # -----------------------------------------------------------------------
    # A09 - ChatGPT file upload / message attachment
    # -----------------------------------------------------------------------
    {
        "rule_id": "R-FILE-UPLOAD-01",
        "behavior_id": "B09",
        "behavior_name": "ChatGPT 파일 업로드 기록",
        "mode": "event_range",

        "start": {
            "ref": "file_object",
            "artifact": "cache",
            "fact_type_in": ["file_metadata", "library_file"],
            "match": {
                "file_id": {"regex": r".+"},
                "filename": {"regex": r".+"},
            },
            "extract": [
                "file_id", "filename", "mime_type", "size", "width", "height",
                "library_file_id", "file_upload_time", "file_processed_time",
                "origination_message_id", "is_library_file",
            ],
            "required": True,
        },

        "confidence": [
            {
                "level": "confirmed",
                "when": {
                    "all_refs": ["file_object"],
                    "field": "file_object.file_upload_time",
                    "regex": r".+",
                },
            },
            {
                "level": "presumed",
                "when": {"all_refs": ["file_object"]},
            },
        ],

        "alternatives": [
            "기존 파일 라이브러리 항목 조회",
            "메시지 전송 전 파일 선택 또는 미리보기",
        ],

        "notes": [
            "An explicit service file_upload_time confirms that the service records an upload, but it is not the captured modifying HTTP request itself.",
            "A /files/<id>/simple object without file_upload_time is only presumed upload evidence.",
            "Do not claim that the file was sent with a prompt unless a conversation_attachment observation exists.",
        ],
    },

    {
        "rule_id": "R-FILE-ATTACHMENT-01",
        "behavior_id": "B09",
        "behavior_name": "ChatGPT 사용자 메시지 파일 첨부",
        "mode": "event_range",

        "start": {
            "ref": "message_attachment",
            "artifact": "cache",
            "fact_type": "conversation_attachment",
            "match": {
                "message_role": {"eq": "user"},
                "source": {"eq": "local"},
                "file_id": {"regex": r".+"},
            },
            "extract": [
                "conversation_id", "message_id", "message_create_time",
                "request_id", "turn_id", "turn_exchange_id", "file_id",
                "filename", "mime_type", "size", "width", "height",
                "source", "library_file_id", "prompt_text",
            ],
            "required": True,
        },

        "confidence": [
            {
                "level": "confirmed",
                "when": {
                    "all": [
                        {"all_refs": ["message_attachment"]},
                        {"field": "message_attachment.conversation_id", "regex": r".+"},
                        {"field": "message_attachment.message_id", "regex": r".+"},
                        {"field": "message_attachment.filename", "regex": r".+"},
                        {"field": "message_attachment.mime_type", "regex": r".+"},
                    ],
                },
            },
            {
                "level": "presumed",
                "when": {"all_refs": ["message_attachment"]},
            },
        ],

        "notes": [
            "A source=local attachment in a recovered user message directly links the file to that sent message.",
            "The message create_time is the attachment-message event time; Cache response_time is the later observation time.",
        ],
    },

    # -----------------------------------------------------------------------
    # A10 - ChatGPT file download
    # -----------------------------------------------------------------------
    {
        "rule_id": "R-DOWNLOAD-01",
        "behavior_id": "B10",
        "behavior_name": "ChatGPT 파일 다운로드",
        "mode": "event_range",

        "start": {
            "ref": "download_intent",
            "artifact": "cache",
            "fact_type": "download_request",
            "match": {
                "hostname": {"eq": "chatgpt.com"},
                "path": {
                    "regex": (
                        r"^/backend-api/conversation/"
                        r"(?P<conversation_id>[^/]+)/interpreter/download$"
                    )
                },
                "query.download_intent": {"eq": "true"},
            },
            "extract": [
                "conversation_id",
                "message_id",
                "sandbox_path",
            ],
            "required": True,
        },

        "milestones": [
            {
                "ref": "estuary_attachment",
                "artifact": "cache",
                "fact_type": "file_content_reference",
                "match": {
                    "hostname": {"eq": "chatgpt.com"},
                    "path": {"eq": "/backend-api/estuary/content"},
                    "query.id": {"exists": True},
                    "query.cd": {"eq": "attachment"},
                },
                "extract": ["file_id", "filename"],
                "required": False,
            },
            {
                "ref": "browser_download",
                "artifact": "history",
                "fact_type": "browser_download",
                "match": {
                    "tab_url.hostname": {"eq": "chatgpt.com"},
                    "target_path": {"exists": True},
                },
                "extract": [
                    "download_id",
                    "conversation_id",
                    "target_path",
                    "target_filename",
                    "start_time",
                    "end_time",
                    "received_bytes",
                    "total_bytes",
                    "state",
                    "interrupt_reason",
                    "download_complete",
                ],
                "select": "first_same_ref:download_intent:conversation_id:conversation_id",
                "required": False,
            },
            {
                "ref": "download_url_chain",
                "artifact": "history",
                "fact_type": "download_url_chain",
                "match": {
                    "url.hostname": {"eq": "chatgpt.com"},
                    "url.path": {"eq": "/backend-api/estuary/content"},
                },
                "extract": ["download_id", "file_id", "filename", "url"],
                "select": "first_same_ref:browser_download:download_id:download_id",
                "required": False,
            },
        ],

        "confidence": [
            {
                "level": "confirmed",
                "when": {
                    "all_refs": [
                        "download_intent",
                        "estuary_attachment",
                        "browser_download",
                        "download_url_chain",
                    ],
                    "relations": [
                        "download_intent.conversation_id == browser_download.conversation_id",
                        "estuary_attachment.file_id == download_url_chain.file_id",
                        "browser_download.download_id == download_url_chain.download_id",
                    ],
                    "field": "browser_download.download_complete",
                    "eq": True,
                },
            },
            {
                "level": "confirmed",
                "when": {
                    "all_refs": ["download_intent", "browser_download"],
                    "relations": [
                        "download_intent.conversation_id == browser_download.conversation_id",
                    ],
                    "field": "browser_download.download_complete",
                    "eq": True,
                },
            },
            {
                "level": "suspected",
                "when": {"all_refs": ["download_intent"]},
            },
        ],

        "exclude": [
            {
                "when": {
                    "field": "download_intent.query.download_intent",
                    "eq": "false",
                },
                "reason": "download_intent=false is preview/display/read context, not user download.",
            }
        ],

        "notes": [
            "download_intent=true alone proves intent only; a completed Chrome downloads row proves completion.",
            "state=1, interrupt_reason=0 and received_bytes=total_bytes are used together for completion.",
            "Recovered body/type is File Context and supports file identity, not download completion by itself.",
        ],
    },

    # -----------------------------------------------------------------------
    # A11 - Conversation removed from the active conversation list
    # -----------------------------------------------------------------------
    {
        "rule_id": "R-CONVERSATION-ACTIVE-REMOVAL-01",
        "behavior_id": "B11",
        "behavior_name": "ChatGPT 대화 활성 목록 제거",
        "mode": "event_range",

        "start": {
            "ref": "previous_active_state",
            "artifact": "local_storage",
            "fact_type": "conversation_history_state",
            "match": {
                "membership": {"eq": True},
                "transition_candidate": {"eq": True},
                "conversation_id": {"exists": True},
            },
            "extract": [
                "conversation_id", "title", "user_id", "workspace_account_id",
                "leveldb_seq_number",
            ],
            "required": True,
        },

        "end": {
            "ref": "current_absent_state",
            "artifact": "local_storage",
            "fact_type": "conversation_history_state",
            "match": {
                "membership": {"eq": False},
                "previous_membership": {"eq": True},
                "transition_type": {"eq": "removed_from_active_list"},
            },
            "extract": [
                "conversation_id", "title", "user_id", "workspace_account_id",
                "previous_leveldb_seq_number", "current_leveldb_seq_number",
                "previous_observed_at", "current_observed_at",
            ],
            "select": "first_same_ref:previous_active_state:conversation_id:conversation_id",
            "required": True,
        },

        "association": [
            {
                "policy": "same_conversation_id",
                "left": "previous_active_state",
                "right": "current_absent_state",
                "required": True,
            },
        ],

        "confidence": [
            {
                "level": "confirmed",
                "when": {
                    "all_refs": ["previous_active_state", "current_absent_state"],
                    "relations": [
                        "previous_active_state.conversation_id == current_absent_state.conversation_id",
                        "previous_active_state.user_id == current_absent_state.user_id",
                        "previous_active_state.workspace_account_id == current_absent_state.workspace_account_id",
                    ],
                },
            },
            {
                "level": "presumed",
                "when": {"all_refs": ["previous_active_state", "current_absent_state"]},
            },
        ],

        "alternatives": [
            "대화 삭제",
            "대화 보관(archive) 또는 기타 활성 목록 제외 상태",
        ],

        "output_extra": {
            "claim_scope": "active-list removal confirmed; deletion completion unverified",
        },

        "notes": [
            "The time range is bounded by the last snapshot containing the conversation and the first snapshot without it; it is not the exact click time.",
            "Do not call this completed deletion without a direct DELETE/result artifact or equivalent server-side proof.",
        ],
    },

    # -----------------------------------------------------------------------
    # Account deletion confirmation/processing flow (not observed in A11)
    # -----------------------------------------------------------------------
    {
        "rule_id": "R-ACCOUNT-DELETE-FLOW-01",
        "behavior_id": "B11",
        "behavior_name": "ChatGPT 계정 삭제 확인/처리 흐름",
        "mode": "event_range",

        "start": {
            "ref": "account_settings",
            "artifact": "history",
            "fact_type": "url_visit",
            "match": {
                "hostname": {"eq": "chatgpt.com"},
                "fragment_route": {"regex": r"^settings/Account(?:$|/)"},
            },
            "required": False,
        },

        "milestones": [
            {
                "ref": "delete_pending",
                "artifact": "session_storage",
                "fact_type": "kv_record",
                "match": {
                    "key": {"eq": "chatgpt_delete_account_pending_confirm"},
                },
                "required": False,
            },
            {
                "ref": "reauth",
                "artifact": "history",
                "fact_type": "url_visit",
                "match": {
                    "hostname": {"eq": "auth.openai.com"},
                    "path": {"eq": "/api/accounts/authorize"},
                    "query.reauth": {"eq": "password"},
                    "query.force_challenge": {"eq": "true"},
                },
                "required": False,
            },
            {
                "ref": "delete_logout_history",
                "artifact": "history",
                "fact_type": "url_visit",
                "match": {
                    "hostname": {"eq": "chatgpt.com"},
                    "path": {"eq": "/auth/logout"},
                },
                "required": False,
            },
        ],

        "end": {
            "ref": "delete_logout_marker",
            "artifact": "cache",
            "fact_type": "http_cache_entry",
            "match": {
                "hostname": {"eq": "chatgpt.com"},
                "path": {"eq": "/auth/logout"},
                "logout_reason": {"eq": "confirm_delete_account"},
                "logout_source": {
                    "eq": "settings.confirm-delete-account-modal"
                },
            },
            "required": True,
        },

        "confidence": [
            {
                "level": "confirmed",
                "when": {
                    "all_refs": ["delete_logout_marker"],
                    "any_refs": [
                        "delete_pending",
                        "account_settings",
                        "reauth",
                        "delete_logout_history",
                    ],
                },
            },
            {
                "level": "suspected",
                "when": {"all_refs": ["delete_pending"]},
            },
        ],

        "output_extra": {
            "completion": "unverified",
        },

        "notes": [
            "This rule confirms the deletion confirmation/processing flow, not server-side account deletion completion.",
            "Client cleanup such as accountSwitchSessions=[] is supporting context only.",
        ],
    },

    # -----------------------------------------------------------------------
    # A12 - Temporary Chat mode opened
    # -----------------------------------------------------------------------
    {
        "rule_id": "R-TEMPCHAT-OPEN-01",
        "behavior_id": "B12",
        "behavior_name": "Temporary Chat 모드 진입",
        "mode": "event_range",

        "start": {
            "ref": "tempchat_history",
            "artifact": "history",
            "fact_type": "url_visit",
            "match": {
                "hostname": {"eq": "chatgpt.com"},
                "query.temporary-chat": {"eq": "true"},
            },
            "required": True,
        },

        "milestones": [
            {
                "ref": "tempchat_route_data",
                "artifact": "cache",
                "fact_type": "http_cache_entry",
                "match": {
                    "hostname": {"eq": "chatgpt.com"},
                    "query.temporary-chat": {"eq": "true"},
                },
                "required": False,
            },
        ],

        "confidence": [
            {
                "level": "confirmed",
                "when": {"all_refs": ["tempchat_history"]},
            },
        ],

        "notes": [
            "Do not hard-code a locale such as /ko-KR/; the query marker is the primary condition.",
        ],
    },

    # -----------------------------------------------------------------------
    # A12 - Temporary Chat actually used
    # -----------------------------------------------------------------------
    {
        "rule_id": "R-TEMPCHAT-USE-01",
        "behavior_id": "B12",
        "behavior_name": "Temporary Chat 실제 사용",
        "mode": "event_range",
        "evidence_window": {"enabled": False},

        "start": {
            "ref": "tempchat_history",
            "artifact": "history",
            "fact_type": "url_visit",
            "match": {
                "hostname": {"eq": "chatgpt.com"},
                "query.temporary-chat": {"eq": "true"},
            },
            "required": True,
        },

        "milestones": [
            {
                "ref": "tempchat_route_data",
                "artifact": "cache",
                "fact_type": "http_cache_entry",
                "match": {
                    "hostname": {"eq": "chatgpt.com"},
                    "query.temporary-chat": {"eq": "true"},
                },
                "required": False,
            },
            {
                "ref": "history_off_cookie",
                "artifact": "cookies",
                "fact_type": "cookie_record",
                "match": {
                    "name": {
                        "regex": r"^history_off_(?P<conversation_id>.+)$"
                    },
                },
                "extract": ["conversation_id"],
                "required": True,
            },
            {
                "ref": "conv_key_cookie",
                "artifact": "cookies",
                "fact_type": "cookie_record",
                "match": {
                    "name": {
                        "regex": r"^conv_key_(?P<conversation_id>.+)$"
                    },
                },
                "extract": ["conversation_id"],
                "select": "first_same_ref:history_off_cookie:conversation_id:conversation_id",
                "required": False,
            },
        ],

        "end": {
            "ref": "tempchat_stream",
            "artifact": "cache",
            "fact_type": "conversation_stream_status",
            "match": {
                "hostname": {"eq": "chatgpt.com"},
                "path": {
                    "regex": (
                        r"^/backend-api/conversation/"
                        r"(?P<conversation_id>[^/]+)/stream_status$"
                    )
                },
                "json.status": {"eq": "IS_STREAMING"},
            },
            "extract": ["conversation_id"],
            "select": "first_same_ref:history_off_cookie:conversation_id:conversation_id",
            "required": True,
        },

        "association": [
            {
                "policy": "same_conversation_id",
                "left": "history_off_cookie",
                "right": "tempchat_stream",
                "required": True,
            },
            {
                "policy": "same_conversation_id",
                "left": "history_off_cookie",
                "right": "conv_key_cookie",
                "required": False,
            },
        ],

        "confidence": [
            {
                "level": "confirmed",
                "when": {
                    "all_refs": [
                        "tempchat_history",
                        "history_off_cookie",
                        "tempchat_stream",
                    ],
                    "relations": [
                        "history_off_cookie.conversation_id == tempchat_stream.conversation_id"
                    ],
                },
            },
        ],

        "suppresses": [
            {
                "rule_id": "R-TEMPCHAT-OPEN-01",
                "scope": "same_flow",
                "reason": "Use the more specific Temporary Chat usage inference when available.",
            }
        ],

        "notes": [
            "history_off_<id> and IS_STREAMING must carry the same conversation ID; route entry alone proves only that the mode was opened.",
            "conv_key_<id> and route-data Cache are corroborating evidence for the standard grade.",
            "Absence of /c/<id> History or conversation-history is Context, not a required condition.",
            "A direct conversation snapshot with is_temporary_chat=true and is_do_not_remember=true would be stronger evidence, but was not recovered in A12.",
        ],
    },

    # -----------------------------------------------------------------------
    # A13-1 - Google Connector link
    # -----------------------------------------------------------------------
    {
        "rule_id": "R-GOOGLE-CONNECTOR-LINK-01",
        "behavior_id": "B13",
        "behavior_name": "Google Connector 연동",
        "mode": "event_range",
        "evidence_window": {"enabled": False},

        "start": {
            "ref": "google_oauth_start",
            "artifact": "history",
            "fact_type": "url_visit",
            "match": {
                "hostname": {"eq": "accounts.google.com"},
                "path": {"prefix": "/o/oauth2/"},
                "query.redirect_uri": {
                    "eq": "https://chatgpt.com/connector_platform_oauth_redirect"
                },
                "query.state": {"exists": True},
            },
            "extract": ["query.state", "query.scope", "query.client_id"],
            "required": True,
        },

        "end": {
            "ref": "connector_callback",
            "artifact": "history",
            "fact_type": "url_visit",
            "match": {
                "hostname": {"eq": "chatgpt.com"},
                "path": {"eq": "/connector_platform_oauth_redirect"},
                "query.code": {"exists": True},
                "query.state": {"exists": True},
            },
            "extract": ["query.state", "query.scope"],
            "required": True,
        },

        "milestones": [
            {
                "ref": "connector_backend",
                "artifact": "cache",
                "fact_type": "http_cache_entry",
                "match": {
                    "hostname": {"eq": "chatgpt.com"},
                    "path": {
                        "regex": r"^/backend-api/(?:aip/connectors/|ps/plugins/plugin_connector_)"
                    },
                },
                "required": False,
            },
        ],

        "association": [
            {
                "left": "google_oauth_start",
                "right": "connector_callback",
                "relation": "google_oauth_start.query.state == connector_callback.query.state",
                "required": True,
            },
        ],

        "confidence": [
            {
                "level": "confirmed",
                "when": {
                    "all_refs": ["google_oauth_start", "connector_callback"],
                    "relations": [
                        "google_oauth_start.query.state == connector_callback.query.state"
                    ],
                },
            },
        ],

        "notes": [
            "Do not call this 'Drive-only link'; the OAuth bundle may include several Google services.",
            "Drive/Gmail/etc. scopes should be displayed as Context.",
            "Connector callback path is distinct from ChatGPT login callback /api/auth/callback/openai.",
        ],
    },

    # -----------------------------------------------------------------------
    # A13-2 - Google Drive connector task with an external result link
    # -----------------------------------------------------------------------
    {
        "rule_id": "R-GOOGLE-DRIVE-CONNECTOR-RESULT-01",
        "behavior_id": "B13",
        "behavior_name": "Google Drive Connector 작업 결과 링크 생성",
        "mode": "event_range",

        "start": {
            "ref": "drive_draft",
            "artifact": "local_storage",
            "fact_type": "conversation_draft",
            "match": {
                "draft_text": {
                    "regex": r"(?is)(?=.*(?:google\s*drive|구글\s*드라이브))(?=.*(?:찾|검색|search))(?=.*(?:업로드|upload))"
                },
            },
            "extract": ["draft_text", "user_id"],
            "required": True,
        },

        "milestones": [
            {
                "ref": "drive_conversation",
                "artifact": "history",
                "fact_type": "conversation_visit",
                "match": {
                    "hostname": {"eq": "chatgpt.com"},
                    "conversation_id": {"exists": True},
                    "title": {"regex": r"(?i)(?:드라이브|drive)"},
                },
                "extract": ["conversation_id", "title"],
                "required": True,
                "select": "first_after_start",
            },
            {
                "ref": "drive_stream",
                "artifact": "cache",
                "fact_type": "conversation_stream_status",
                "match": {
                    "json.status": {"eq": "IS_STREAMING"},
                },
                "extract": ["conversation_id", "json.status"],
                "required": False,
                "select": "first_same_ref:drive_conversation:conversation_id",
            },
        ],

        "end": {
            "ref": "drive_result_link",
            "artifact": "cache",
            "fact_type": "url_safety_check",
            "match": {
                "target_hostname": {"eq": "drive.google.com"},
                "conversation_id": {"exists": True},
                "external_file_id": {"exists": True},
            },
            "extract": [
                "conversation_id", "target_url", "target_hostname",
                "external_file_id",
            ],
            "required": True,
            "select": "first_same_ref:drive_conversation:conversation_id",
        },

        "association": [
            {
                "policy": "same_conversation_id",
                "left": "drive_conversation",
                "right": "drive_stream",
                "required": False,
            },
            {
                "policy": "same_conversation_id",
                "left": "drive_conversation",
                "right": "drive_result_link",
                "required": True,
            },
        ],

        "confidence": [
            {
                "level": "confirmed",
                "when": {
                    "all_refs": [
                        "drive_draft", "drive_conversation", "drive_stream",
                        "drive_result_link",
                    ],
                },
            },
            {
                "level": "presumed",
                "when": {
                    "all_refs": [
                        "drive_draft", "drive_conversation", "drive_result_link",
                    ],
                },
            },
        ],

        "alternatives": [
            "기존 Google Drive 파일 링크를 응답에서 재표시",
            "신규 업로드가 아닌 Drive 검색·읽기 결과 링크",
        ],
        "notes": [
            "The rule confirms a Drive-oriented prompt produced a streamed conversation result containing a Drive file link.",
            "A URL safety check is a rendered-result trace; it is not a provider-side upload success receipt.",
            "Do not label the exact search, read, create or upload sub-action as confirmed without a tool request/result or provider audit record.",
        ],
    },
]


# Evidence grades are profile decisions, not artifact-type aliases.  The
# engine checks these after the base rule has bound and emits exactly one row
# with the highest satisfied grade.  "unavailable" records that an evidence
# grade has not yet been validated instead of fabricating a condition.
RULE_LEVELS: Final[dict[str, dict[str, Any]]] = {
    "R-ACCESS-01": {
        "minimum": {"rule_id": "R-ACCESS-MIN-01", "when": {"all_refs": ["access_history"]}},
        "standard": {"rule_id": "R-ACCESS-STD-01", "when": {"all_refs": ["access_history", "unauth_cache"]}},
        "extended": {"rule_id": "R-ACCESS-EXT-01", "status": "unavailable"},
    },
    "R-LOGIN-01": {
        "minimum": {"rule_id": "R-LOGIN-MIN-01", "when": {"all_refs": ["login_start", "authorize", "login_callback"]}},
        "standard": {"rule_id": "R-LOGIN-STD-01", "when": {"all_refs": ["login_start", "authorize", "login_callback", "me_after_login"]}},
        "extended": {"rule_id": "R-LOGIN-EXT-01", "status": "unavailable"},
    },
    "R-LOGOUT-01": {
        "minimum": {"rule_id": "R-LOGOUT-MIN-01", "when": {"all_refs": ["logout_history"]}},
        "standard": {"rule_id": "R-LOGOUT-STD-01", "when": {"all_refs": ["logout_history"], "any_refs": ["return_home", "logout_marker"]}},
        "extended": {"rule_id": "R-LOGOUT-EXT-01", "when": {"all_refs": ["logout_history", "signout_state"]}},
    },
    "R-ACCOUNT-SWITCH-01": {
        "minimum": {"rule_id": "R-ACCOUNT-SWITCH-MIN-01", "when": {"all_refs": ["switch_history"]}},
        "standard": {"rule_id": "R-ACCOUNT-SWITCH-STD-01", "when": {"all_refs": ["switch_history"], "any_refs": ["switch_session", "me_after_switch"]}},
        "extended": {"rule_id": "R-ACCOUNT-SWITCH-EXT-01", "when": {"all_refs": ["switch_history", "switch_session", "me_after_switch"]}},
    },
    "R-CONVERSATION-CREATE-01": {
        "minimum": {"rule_id": "R-CONVERSATION-CREATE-MIN-01", "when": {"all_refs": ["web_route"]}},
        "standard": {"rule_id": "R-CONVERSATION-CREATE-STD-01", "when": {"all_refs": ["web_route", "create_stream", "canonical_history"]}},
        "extended": {"rule_id": "R-CONVERSATION-CREATE-EXT-01", "when": {"all_refs": ["web_route", "create_stream", "canonical_history", "null_thread_draft"]}},
    },
    "R-PROMPT-EDIT-01": {
        "minimum": {"rule_id": "R-PROMPT-EDIT-MIN-01", "when": {"all_refs": ["before_snapshot", "after_snapshot"]}},
        "standard": {"rule_id": "R-PROMPT-EDIT-STD-01", "when": {"all_refs": ["before_snapshot", "edit_stream", "after_snapshot"]}},
        "extended": {"rule_id": "R-PROMPT-EDIT-EXT-01", "status": "unavailable"},
    },
    "R-MEMORY-MANAGEMENT-OPEN-01": {
        "minimum": {"rule_id": "R-MEMORY-MANAGEMENT-OPEN-MIN-01", "when": {"all_refs": ["memory_settings"]}},
        "standard": {"rule_id": "R-MEMORY-MANAGEMENT-OPEN-STD-01", "when": {"all_refs": ["memory_settings"], "any_refs": ["memory_section", "memory_api"]}},
        "extended": {"rule_id": "R-MEMORY-MANAGEMENT-OPEN-EXT-01", "when": {"all_refs": ["memory_settings", "memory_section", "memory_api"]}},
    },
    "R-MEMORY-STATE-CHANGE-01": {
        "minimum": {
            "rule_id": "R-MEMORY-STATE-CHANGE-MIN-01",
            "when": {"all_refs": ["before_memory_checksum", "after_memory_checksum"]},
        },
        "standard": {
            "rule_id": "R-MEMORY-STATE-CHANGE-STD-01",
            "when": {
                "all": [
                    {"all_refs": ["before_memory_checksum", "after_memory_checksum"]},
                    {"field": "after_memory_checksum.is_stale", "eq": False},
                ],
                "relations": [
                    "after_memory_checksum.cached_source_checksum == after_memory_checksum.source_checksum",
                    "before_memory_checksum.cached_source_checksum != after_memory_checksum.cached_source_checksum",
                ],
            },
        },
        "extended": {"rule_id": "R-MEMORY-STATE-CHANGE-EXT-01", "status": "unavailable"},
    },
    "R-FILE-UPLOAD-01": {
        "minimum": {
            "rule_id": "R-FILE-UPLOAD-MIN-01",
            "when": {"all_refs": ["file_object"]},
        },
        "standard": {
            "rule_id": "R-FILE-UPLOAD-STD-01",
            "when": {
                "all_refs": ["file_object"],
                "field": "file_object.file_upload_time",
                "regex": r".+",
            },
        },
        "extended": {"rule_id": "R-FILE-UPLOAD-EXT-01", "status": "unavailable"},
    },
    "R-FILE-ATTACHMENT-01": {
        "minimum": {
            "rule_id": "R-FILE-ATTACHMENT-MIN-01",
            "when": {"all_refs": ["message_attachment"]},
        },
        "standard": {
            "rule_id": "R-FILE-ATTACHMENT-STD-01",
            "when": {
                "all": [
                    {"all_refs": ["message_attachment"]},
                    {"field": "message_attachment.conversation_id", "regex": r".+"},
                    {"field": "message_attachment.message_id", "regex": r".+"},
                    {"field": "message_attachment.filename", "regex": r".+"},
                    {"field": "message_attachment.mime_type", "regex": r".+"},
                ],
            },
        },
        "extended": {"rule_id": "R-FILE-ATTACHMENT-EXT-01", "status": "unavailable"},
    },
    "R-DOWNLOAD-01": {
        "minimum": {"rule_id": "R-DOWNLOAD-MIN-01", "when": {"all_refs": ["download_intent"]}},
        "standard": {
            "rule_id": "R-DOWNLOAD-STD-01",
            "when": {
                "all_refs": ["download_intent", "browser_download"],
                "relations": [
                    "download_intent.conversation_id == browser_download.conversation_id",
                ],
                "field": "browser_download.download_complete",
                "eq": True,
            },
        },
        "extended": {
            "rule_id": "R-DOWNLOAD-EXT-01",
            "when": {
                "all_refs": ["download_intent", "estuary_attachment", "browser_download", "download_url_chain"],
                "relations": [
                    "download_intent.conversation_id == browser_download.conversation_id",
                    "estuary_attachment.file_id == download_url_chain.file_id",
                    "browser_download.download_id == download_url_chain.download_id",
                ],
                "field": "browser_download.download_complete",
                "eq": True,
            },
        },
    },
    "R-CONVERSATION-ACTIVE-REMOVAL-01": {
        "minimum": {
            "rule_id": "R-CONVERSATION-ACTIVE-REMOVAL-MIN-01",
            "when": {"all_refs": ["previous_active_state", "current_absent_state"]},
        },
        "standard": {
            "rule_id": "R-CONVERSATION-ACTIVE-REMOVAL-STD-01",
            "when": {
                "all_refs": ["previous_active_state", "current_absent_state"],
                "relations": [
                    "previous_active_state.conversation_id == current_absent_state.conversation_id",
                    "previous_active_state.user_id == current_absent_state.user_id",
                    "previous_active_state.workspace_account_id == current_absent_state.workspace_account_id",
                ],
            },
        },
        "extended": {
            "rule_id": "R-CONVERSATION-ACTIVE-REMOVAL-EXT-01",
            "status": "unavailable",
        },
    },
    "R-ACCOUNT-DELETE-FLOW-01": {
        "minimum": {"rule_id": "R-ACCOUNT-DELETE-FLOW-MIN-01", "when": {"all_refs": ["delete_logout_marker"]}},
        "standard": {"rule_id": "R-ACCOUNT-DELETE-FLOW-STD-01", "when": {"all_refs": ["delete_logout_marker"], "any_refs": ["delete_pending", "account_settings", "reauth", "delete_logout_history"]}},
        "extended": {"rule_id": "R-ACCOUNT-DELETE-FLOW-EXT-01", "when": {"all_refs": ["delete_logout_marker", "delete_pending", "account_settings", "reauth", "delete_logout_history"]}},
    },
    "R-TEMPCHAT-OPEN-01": {
        "minimum": {"rule_id": "R-TEMPCHAT-OPEN-MIN-01", "when": {"all_refs": ["tempchat_history"]}},
        "standard": {"rule_id": "R-TEMPCHAT-OPEN-STD-01", "when": {"all_refs": ["tempchat_history", "tempchat_route_data"]}},
        "extended": {"rule_id": "R-TEMPCHAT-OPEN-EXT-01", "status": "unavailable"},
    },
    "R-TEMPCHAT-USE-01": {
        "minimum": {"rule_id": "R-TEMPCHAT-USE-MIN-01", "when": {"all_refs": ["tempchat_history", "history_off_cookie", "tempchat_stream"]}},
        "standard": {"rule_id": "R-TEMPCHAT-USE-STD-01", "when": {"all_refs": ["tempchat_history", "tempchat_route_data", "history_off_cookie", "tempchat_stream", "conv_key_cookie"]}},
        "extended": {"rule_id": "R-TEMPCHAT-USE-EXT-01", "status": "unavailable"},
    },
    "R-GOOGLE-CONNECTOR-LINK-01": {
        "minimum": {"rule_id": "R-GOOGLE-CONNECTOR-LINK-MIN-01", "when": {"all_refs": ["google_oauth_start", "connector_callback"]}},
        "standard": {"rule_id": "R-GOOGLE-CONNECTOR-LINK-STD-01", "when": {"all_refs": ["google_oauth_start", "connector_callback", "connector_backend"]}},
        "extended": {"rule_id": "R-GOOGLE-CONNECTOR-LINK-EXT-01", "status": "unavailable"},
    },
    "R-GOOGLE-DRIVE-CONNECTOR-RESULT-01": {
        "minimum": {"rule_id": "R-GOOGLE-DRIVE-CONNECTOR-RESULT-MIN-01", "when": {"all_refs": ["drive_draft", "drive_conversation", "drive_result_link"]}},
        "standard": {"rule_id": "R-GOOGLE-DRIVE-CONNECTOR-RESULT-STD-01", "when": {"all_refs": ["drive_draft", "drive_conversation", "drive_stream", "drive_result_link"]}},
        "extended": {"rule_id": "R-GOOGLE-DRIVE-CONNECTOR-RESULT-EXT-01", "status": "unavailable"},
    },
}


# ---------------------------------------------------------------------------
# 5. Internal / contextual event rules
# ---------------------------------------------------------------------------
#
# These rules surface useful facts but should not automatically create a row in
# behaviors.csv unless the common behavior taxonomy is explicitly extended.

CONTEXT_RULES: Final[list[dict[str, Any]]] = [

    # A06 - Existing conversation opened
    {
        "rule_id": "R-CONVERSATION-OPEN-01",
        "context_type": "conversation_open",
        "label": "기존 ChatGPT 대화 호출",

        "match": [
            {
                "ref": "conversation_history",
                "artifact": "history",
                "fact_type": "conversation_visit",
                "match": {
                    "hostname": {"eq": "chatgpt.com"},
                    "path": {
                        "regex": r"^/c/(?P<conversation_id>(?!WEB:)[^/?#]+)$"
                    },
                },
                "extract": ["conversation_id"],
                "required": True,
            },
            {
                "ref": "conversation_load",
                "artifact": "cache",
                "fact_type_in": ["conversation_load", "conversation_snapshot"],
                "match": {
                    "hostname": {"eq": "chatgpt.com"},
                    "path": {
                        "regex": (
                            r"^/backend-api/conversations/"
                            r"(?P<conversation_id>[^/?#]+)$"
                        )
                    },
                },
                "extract": ["conversation_id"],
                "required": True,
            },
        ],

        "association": [
            {
                "policy": "same_conversation_id",
                "left": "conversation_history",
                "right": "conversation_load",
                "required": True,
            },
        ],

        "confidence": "confirmed",

        "exclude_if_enclosed_by": [
            {
                "rule_id": "R-CONVERSATION-CREATE-01",
                "relation": "same_conversation_flow",
            }
        ],

        "notes": [
            "Canonical /c/<id> alone cannot distinguish newly created conversation from existing conversation open.",
        ],
    },

    # A06/A07 - Prompt draft
    {
        "rule_id": "CTX-PROMPT-DRAFT-01",
        "context_type": "prompt_draft",
        "label": "Prompt 초안 작성 흔적",
        "match": [
            {
                "ref": "draft",
                "artifact": "local_storage",
                "fact_type": "conversation_draft",
                "match": {
                    "draft_text": {"exists": True},
                },
                "required": True,
            }
        ],
        "extract": [
            "conversation_id",
            "draft_id",
            "draft_text",
            "residual_sequence",
        ],
        "notes": [
            "A draft does not prove Send/Submit.",
            "Multiple residual versions may show intermediate typing states.",
        ],
    },

    {
        "rule_id": "CTX-CONVERSATION-SNAPSHOT-01",
        "context_type": "conversation_snapshot",
        "label": "복원된 대화 스냅샷",
        "match_any": [
            {
                "ref": "snapshot",
                "artifact": "cache",
                "fact_type": "conversation_snapshot",
            }
        ],
        "extract": [
            "conversation_id",
            "title",
            "current_node",
            "message_count",
            "message_ids",
            "user_message_ids",
            "is_temporary_chat",
            "is_do_not_remember",
        ],
        "notes": [
            "Conversation and message identifiers are forensic context even when no action can be inferred.",
        ],
    },

    {
        "rule_id": "CTX-CONVERSATION-HISTORY-ITEM-01",
        "context_type": "conversation_history_item",
        "label": "대화 목록에서 복원된 대화 정보",
        "match_any": [
            {
                "ref": "history_item",
                "artifact": "local_storage",
                "fact_type": "conversation_history_state",
                "match": {
                    "conversation_id": {"exists": True},
                    "title": {"exists": True},
                },
            }
        ],
        "extract": [
            "user_id", "workspace_account_id", "conversation_id", "title",
            "create_time", "update_time", "memory_scope", "is_archived",
            "is_temporary_chat", "is_do_not_remember",
        ],
        "notes": [
            "A conversation title can guide review but does not prove the title-described action occurred.",
        ],
    },

    # A06/A07/A12 - Generation
    {
        "rule_id": "CTX-GENERATION-STREAM-01",
        "context_type": "generation_streaming",
        "label": "Generation / Streaming 관찰",
        "match": [
            {
                "ref": "stream",
                "artifact": "cache",
                "fact_type": "conversation_stream_status",
                "match": {
                    "json.status": {"eq": "IS_STREAMING"},
                },
                "required": True,
            }
        ],
        "extract": ["conversation_id"],
        "notes": [
            "IS_STREAMING does not identify whether the cause was a new prompt, prompt edit, or response regeneration.",
        ],
    },

    # A04 - Account observation lane
    {
        "rule_id": "CTX-ACCOUNT-OBSERVED-01",
        "context_type": "account_observation",
        "label": "관찰된 ChatGPT 계정",
        "match": [
            {
                "ref": "account",
                "artifact": "cache",
                "fact_type": "account_observation",
                "match": {
                    "account_id": {"exists": True},
                },
                "required": True,
            }
        ],
        "extract": [
            "account_id", "email", "name", "first_name", "phone_number",
            "country", "region", "region_code", "client_id",
            "email_domain_type", "ads_segment_id", "organization_ids",
            "organization_names", "address", "city", "postal_code",
        ],
        "notes": [
            "An observed account is not automatically 'the account of this PC'.",
        ],
    },

    {
        "rule_id": "CTX-ACCOUNT-SESSION-OBSERVED-01",
        "context_type": "account_session_observation",
        "label": "Local Storage에서 관찰된 계정 세션 정보",
        "match_any": [
            {
                "ref": "account_session",
                "artifact": "local_storage",
                "fact_type": "account_session_observation",
            }
        ],
        "extract": [
            "account_id", "user_id", "email", "email_domain_type",
            "phone_number", "name", "auth_provider", "last_logged_in_at",
            "workspace_account_ids", "workspace_names", "user_image_url",
            "has_session_token",
        ],
        "notes": [
            "Session tokens remain only in raw evidence and are not copied into semantic fields.",
            "The observed account session is not automatically the owner of the device.",
        ],
    },

    # A09 - File metadata / body / attachment
    {
        "rule_id": "R-FILE-ACTIVITY-01",
        "context_type": "file_activity",
        "behavior_hint": "B09",
        "label": "파일/이미지 관련 활동",
        "match_any": [
            {
                "artifact": "cache",
                "fact_type": "file_metadata",
            },
            {
                "artifact": "cache",
                "fact_type": "file_content_reference",
            },
            {
                "artifact": "cache",
                "fact_type": "file_content",
            },
            {
                "artifact": "cache",
                "fact_type": "conversation_attachment",
            },
            {
                "artifact": "cache",
                "fact_type": "library_file",
            },
        ],
        "association_preference": [
            "same_file_id",
            "same_message_id",
            "same_conversation_id",
        ],
        "extract": [
            "file_id", "filename", "mime_type", "size", "width", "height",
            "sha256", "content_type_detected", "body_recovered", "raw_dataset",
            "text_encoding", "line_count", "python_source_detected",
            "file_upload_time", "file_processed_time", "library_file_id",
            "origination_message_id", "conversation_id", "message_id",
            "message_role", "message_create_time", "source", "access_mode",
            "download_intent", "status",
        ],
        "notes": [
            "Direct conversation/message attachment proves linkage, not necessarily that the upload action happened during this acquisition window.",
            "download_intent=false is preview/display/read context and must not be treated as actual download.",
            "Do not claim 'prompt sent with this file' unless message-level attachment evidence actually supports it.",
        ],
    },

    # A08 - Memory API details
    {
        "rule_id": "CTX-MEMORY-STATE-01",
        "context_type": "memory_state",
        "label": "Memory 관련 상태/조회",
        "match_any": [
            {
                "ref": "memory_list",
                "artifact": "cache",
                "fact_type": "memory_list_snapshot",
            },
            {
                "ref": "memory_checksum",
                "artifact": "cache",
                "fact_type": "memory_summary_checksum",
            },
            {
                "ref": "memory_correction_capability",
                "artifact": "cache",
                "fact_type": "memory_correction_capability",
            },
            {
                "ref": "memory_api_fallback",
                "artifact": "cache",
                "fact_type": "http_cache_entry",
                "match": {
                    "path": {
                        "regex": r"^/backend-api/memories(?:$|\?|/)"
                    },
                },
            }
        ],
        "extract": [
            "memory_count", "memory_entries", "memory_num_tokens",
            "memory_max_tokens", "includes_memory_entries",
            "cached_generated_at", "cached_source_checksum", "source_checksum",
            "is_stale", "schema_version", "correction_actions",
        ],
        "notes": [
            "Do not classify edit/delete from endpoint names alone.",
            "Correction actions describe available UI capability, not proof that correction was executed.",
        ],
    },

    # A10 - Recovered downloaded file
    {
        "rule_id": "CTX-DOWNLOAD-FILE-01",
        "context_type": "recovered_file",
        "label": "복원된 다운로드 파일",
        "match": [
            {
                "ref": "file_body",
                "fact_type": "file_content",
                "match": {
                    "body_recovered": {"eq": True},
                },
                "required": True,
            }
        ],
        "extract": [
            "file_id",
            "filename",
            "mime_type",
            "size",
            "sha256",
            "content_type_detected",
            "text_encoding",
            "line_count",
            "python_source_detected",
            "raw_dataset",
        ],
    },

    # Sign-out cleanup. This is not account-deletion proof by itself.
    {
        "rule_id": "CTX-SIGNOUT-CLEANUP-01",
        "context_type": "signout_cleanup",
        "label": "로그아웃 후 클라이언트 상태 정리",
        "match_any": [
            {
                "artifact": "local_storage",
                "fact_type": "kv_record",
                "match": {
                    "key": {"eq": "oai/apps/accountSwitchSessions"},
                    "json": {"eq": []},
                },
            },
            {
                "artifact": "local_storage",
                "fact_type": "deletion_record",
                "match": {
                    "key": {
                        "regex": r"^cache/user-.+/(?:conversation-history|models|pinned-items|snorlax-history)"
                    },
                },
            },
            {
                "artifact": "local_storage",
                "fact_type": "kv_record",
                "match": {
                    "key": {"eq": "nextauth.message"},
                    "json.data.trigger": {"eq": "signout"},
                },
            },
        ],
        "notes": [
            "Ordinary logout, account switching, and account-delete flows may all leave client cleanup records.",
            "Client cleanup alone does not prove account deletion or server-side completion.",
        ],
    },

    # A12 - Temporary Chat negative/absence context is intentionally NOT a rule.
    # Absence findings should be generated only by a report/snapshot layer that knows
    # the observation search scope and can truthfully say 'not observed'.

    # A13-2 - Google Drive connector usage context
    {
        "rule_id": "CTX-GOOGLE-DRIVE-USAGE-01",
        "context_type": "connector_usage_context",
        "label": "Google Drive Connector 사용 정황",
        "match_any": [
            {
                "artifact": "cache",
                "fact_type": "http_cache_entry",
                "match": {
                    "path": {"eq": "/backend-api/system_hints"},
                    "query.mode": {"eq": "plugins"},
                },
            },
            {
                "artifact": "cache",
                "fact_type": "url_safety_check",
                "match": {
                    "target_hostname": {"eq": "drive.google.com"},
                    "conversation_id": {"exists": True},
                },
            },
            {
                "artifact": "cache",
                "fact_type": "resource",
                "match": {
                    "url": {"contains": "/images/ecosystem/apps/google_drive/"},
                },
            },
        ],
        "notes": [
            "These facts support Google Drive Connector usage context, not a specific Drive action execution.",
            "Do not infer read/write/upload solely from /actions capability metadata.",
        ],
    },
    {
        "rule_id": "CTX-GOOGLE-OAUTH-SCOPES-01",
        "context_type": "connector_authorization_context",
        "label": "Google Connector OAuth 권한 범위",
        "match": [
            {
                "ref": "google_oauth",
                "artifact": "history",
                "fact_type": "url_visit",
                "match": {
                    "hostname": {"eq": "accounts.google.com"},
                    "path": {"eq": "/o/oauth2/v2/auth"},
                    "query.redirect_uri": {
                        "eq": "https://chatgpt.com/connector_platform_oauth_redirect"
                    },
                    "query.scope": {"exists": True},
                },
                "extract": ["query.client_id", "query.scope", "query.state"],
                "required": True,
            },
        ],
        "extract": ["query.client_id", "query.scope", "query.state"],
        "notes": [
            "Scopes describe permissions requested by the OAuth bundle, not proof that every Google service was used.",
        ],
    },
    {
        "rule_id": "CTX-GOOGLE-OAUTH-DISPLAY-NAME-01",
        "context_type": "identity_context",
        "label": "Google OAuth 화면 계정 표시명 후보",
        "match": [
            {
                "ref": "google_account_screen",
                "artifact": "history",
                "fact_type": "url_visit",
                "match": {
                    "hostname": {"eq": "accounts.google.com"},
                    "path": {"eq": "/v3/signin/challenge/pwd"},
                    "query.redirect_uri": {
                        "eq": "https://chatgpt.com/connector_platform_oauth_redirect"
                    },
                    "title": {"exists": True},
                },
                "extract": ["title"],
                "required": True,
            },
        ],
        "extract": ["title"],
        "notes": [
            "The History title is an OAuth UI display-name candidate and must not be treated as verified legal identity or device ownership.",
        ],
    },
    {
        "rule_id": "CTX-GMAIL-CONVERSATION-01",
        "context_type": "connector_usage_context",
        "label": "Gmail/이메일 Connector 대화 정황",
        "match": [
            {
                "ref": "gmail_conversation",
                "artifact": "local_storage",
                "fact_type": "conversation_history_state",
                "match": {
                    "title": {"regex": r"(?i)(?:gmail|이메일)"},
                    "conversation_id": {"exists": True},
                },
                "extract": ["conversation_id", "title", "update_time"],
                "required": True,
            },
        ],
        "extract": ["conversation_id", "title", "update_time"],
        "notes": [
            "A generated conversation title supports email-task context but does not prove a Gmail read, reply or send operation.",
        ],
    },
]


# ---------------------------------------------------------------------------
# 6. State-transition rules
# ---------------------------------------------------------------------------

STATE_TRANSITION_RULES: Final[list[dict[str, Any]]] = [

    # A11-1 - Conversation removed from active list
    {
        "rule_id": "R-CONVERSATION-REMOVED-01",
        "context_type": "conversation_removed_from_active_list",
        "label": "활성 대화 목록에서 제거됨",
        "mode": "state_transition",

        "previous": {
            "ref": "previous_active_state",
            "source_preferences": [
                "local_storage_residual",
                "cache_residual",
            ],
            "fact_type_in": [
                "conversation_history_state",
                "active_conversation_list",
            ],
            "requires": {
                "conversation_id": {"exists": True},
                "membership": {"eq": True},
                "transition_candidate": {"eq": True},
            },
        },

        "current": {
            "ref": "current_active_state",
            "source_preferences": [
                "local_storage_transition_current",
                "local_storage_current",
                "cache_current",
            ],
            "fact_type_in": [
                "conversation_history_state",
                "active_conversation_list",
            ],
            "requires": {
                "same_conversation_id_membership": {"eq": False},
            },
        },

        "association": [
            {
                "policy": "same_conversation_id",
                "left": "previous_active_state",
                "right": "current_active_state",
                "required": True,
            }
        ],

        "confidence": "confirmed",

        "higher_level_interpretation": {
            "label": "대화 삭제 정황",
            "confidence": "suspected",
            "alternatives": [
                "archive 또는 기타 active-list 제외 상태",
            ],
        },

        "notes": [
            "Do not emit 'deleted' merely because a conversation is absent from is_archived=false results.",
            "Residual History/Cache may remain after the conversation leaves the active list.",
            "The runtime rule must use previous/residual and current state recovered from the same acquired evidence set.",
        ],
    },
]


# ---------------------------------------------------------------------------
# 7. Cross-rule suppression / precedence
# ---------------------------------------------------------------------------

RULE_PRECEDENCE: Final[list[dict[str, Any]]] = [
    {
        "higher": "R-ACCOUNT-DELETE-FLOW-01",
        "lower": "R-LOGOUT-01",
        "scope": "same_logout_flow",
        "reason": (
            "Delete-account logout markers are more specific than the generic logout rule."
        ),
    },
    {
        "higher": "R-TEMPCHAT-USE-01",
        "lower": "R-TEMPCHAT-OPEN-01",
        "scope": "same_flow",
        "reason": (
            "When actual Temporary Chat usage is confirmed, avoid duplicate B12 rows for open+use."
        ),
    },
    {
        "higher": "R-CONVERSATION-CREATE-01",
        "lower": "R-CONVERSATION-OPEN-01",
        "scope": "same_conversation_flow",
        "reason": (
            "The canonical /c/<id> that closes a new-conversation flow must not be "
            "misclassified as an existing-conversation open."
        ),
    },
]


# ---------------------------------------------------------------------------
# 8. Unresolved / intentionally unsupported behavior claims
# ---------------------------------------------------------------------------

UNRESOLVED_BEHAVIORS: Final[dict[str, dict[str, Any]]] = {
    "prompt_submit": {
        "status": "unresolved",
        "reason": (
            "Draft + IS_STREAMING does not uniquely distinguish new prompt send "
            "from prompt edit or response regeneration."
        ),
        "safe_output": [
            "Prompt draft observed",
            "Generation/Streaming observed",
        ],
    },

    "prompt_edit_single_snapshot": {
        "status": "unresolved",
        "experiment": "A07-1",
        "reason": (
            "A prompt edit is supported only when two recoverable snapshots of the same "
            "conversation prove a user-message branch replacement. One snapshot, "
            "include_has_versions=true, or streaming alone is not unique to prompt editing."
        ),
        "safe_output": [
            "Conversation snapshot observed",
            "Generation/Streaming observed",
        ],
    },

    "response_regenerate": {
        "status": "unresolved",
        "experiment": "separate experiment required",
        "reason": (
            "No runtime-unique artifact distinguished response regeneration from other generation."
        ),
    },

    "memory_edit": {
        "status": "unresolved",
        "experiment": "A08",
        "reason": (
            "A source-checksum transition proves that Memory/About You source state changed, "
            "but the recovered Cache has no modifying request that distinguishes a prompt-based "
            "request from direct Personalization correction."
        ),
        "safe_output": [
            "Memory/Personalization state changed",
            "Exact change method not identified",
        ],
    },

    "memory_delete": {
        "status": "unresolved",
        "experiment": "A08",
        "reason": (
            "No deletion-specific request/result or before/after entry removal was recovered."
        ),
    },

    "file_upload_action": {
        "status": "unresolved",
        "experiment": "A09",
        "behavior_hint": "B09",
        "reason": (
            "An explicit file_upload_time records a service-side upload and a local-source "
            "conversation attachment proves message linkage. A simple file object/body alone "
            "still does not capture the modifying upload request."
        ),
        "safe_output": [
            "Service-recorded file upload when file_upload_time is present",
            "File object observed; upload presumed when upload time is absent",
            "File body observed/recovered",
            "User-message attachment confirmed when directly present",
        ],
    },

    "conversation_delete_completion": {
        "status": "unresolved",
        "experiment": "A11",
        "reason": (
            "Removal from is_archived=false active lists does not uniquely distinguish "
            "deletion from archive or another active-list exclusion state."
        ),
        "safe_output": [
            "Conversation removed from active list",
            "Conversation deletion suspected",
        ],
    },

    "account_delete_completion": {
        "status": "unresolved",
        "experiment": "A11",
        "reason": (
            "The A11 material contains explicit ordinary-logout markers "
            "(logout_confirm_modal / modal.logout-confirm-modal), not the "
            "confirm_delete_account marker. No server-side deletion result was observed."
        ),
        "safe_output": [
            "Ordinary logout/account-switch cleanup observed",
            "Account deletion not observed",
        ],
    },

    "temporary_chat_content_recovery": {
        "status": "unresolved",
        "experiment": "A12",
        "reason": (
            "The route, history-off cookie and matching stream-status confirm Temporary Chat "
            "usage, but no conversation snapshot or message body for that conversation was recovered."
        ),
        "safe_output": [
            "Temporary Chat use confirmed",
            "Prompt, response and completion status not recovered",
        ],
    },

    "connector_provider_subaction_execution": {
        "status": "unresolved",
        "experiment": "A13-2",
        "reason": (
            "A Drive-oriented prompt, matching streamed conversation and rendered Drive file "
            "link were observed, but no provider tool request/result proved whether the link "
            "came from search, read, create or upload.  The Gmail conversation title likewise "
            "does not prove a read, reply or send operation."
        ),
        "safe_output": [
            "Google Drive Connector task result link observed",
            "Exact Drive sub-action and Gmail operation not directly identified",
        ],
    },
}


# ---------------------------------------------------------------------------
# 9. Validation / implementation checklist exposed with the ruleset
# ---------------------------------------------------------------------------

IMPLEMENTATION_REQUIREMENTS: Final[list[str]] = [
    "Parse URLs structurally; do not use substring matching for host identity.",
    "Parse fragment route/query separately from the normal URL query.",
    "Support optional end anchors without inventing when_to.",
    "Support milestone ordering and same-ID associations.",
    "Support cross-rule suppression/precedence.",
    "Support state-transition comparison of residual/previous versus current state.",
    "Compare active conversation branches before classifying prompt edit; never use streaming alone.",
    "Preserve useful identity, account, conversation and message fields as Context even when no behavior matches.",
    "Do not attach ambiguous supporting evidence by nearest timestamp alone.",
    "Preserve Observation IDs in supported_by for every generated inference/context.",
    "Keep existing mandatory CSV/JSONL filenames, columns, ordering and encoding unchanged.",
    "Never emit numeric confidence percentages/scores.",
    "Treat capability discovery (/actions) separately from provider action execution.",
    "Treat download_intent=false separately from actual browser download.",
]


def validate_ruleset_shape() -> None:
    """
    Lightweight draft-time sanity check only.

    This does not validate artifact contents. It catches duplicate IDs,
    confidence vocabulary mistakes, broken evidence references and unknown
    association policies before a case is analyzed.
    """
    ids: list[str] = []
    level_ids: list[str] = []

    for group in (
        INFERENCE_RULES,
        CONTEXT_RULES,
        STATE_TRANSITION_RULES,
    ):
        for rule in group:
            rule_id = rule["rule_id"]
            if rule_id in ids:
                raise ValueError(f"Duplicate rule_id: {rule_id}")
            ids.append(rule_id)

            confidence = rule.get("confidence")
            if isinstance(confidence, str):
                if confidence not in CONFIDENCE_VALUES:
                    raise ValueError(
                        f"{rule_id}: invalid confidence {confidence!r}"
                    )
            elif isinstance(confidence, list):
                for item in confidence:
                    level = item.get("level")
                    if level not in CONFIDENCE_VALUES:
                        raise ValueError(
                            f"{rule_id}: invalid confidence {level!r}"
                        )

    for key, item in UNRESOLVED_BEHAVIORS.items():
        if item.get("status") != "unresolved":
            raise ValueError(
                f"{key}: unresolved behavior entry must have status='unresolved'"
            )

    for rule in INFERENCE_RULES:
        base_id = rule['rule_id']
        specs = []
        if rule.get('start'):
            specs.append(rule['start'])
        specs.extend(rule.get('milestones') or [])
        if rule.get('end'):
            specs.append(rule['end'])
        refs = [item.get('ref') for item in specs if item.get('ref')]
        duplicate_refs = sorted({ref for ref in refs if refs.count(ref) > 1})
        if duplicate_refs:
            raise ValueError(f'{base_id}: duplicate evidence refs {duplicate_refs}')
        known_refs = set(refs)
        for association in rule.get('association') or []:
            policy = association.get('policy')
            relation = association.get('relation')
            if not policy and not relation:
                raise ValueError(
                    f'{base_id}: association requires policy or relation'
                )
            if policy and policy not in ASSOCIATION_POLICIES:
                raise ValueError(
                    f'{base_id}: unknown association policy {policy!r}'
                )
            for side in ('left', 'right'):
                ref = association.get(side)
                if ref not in known_refs:
                    raise ValueError(
                        f'{base_id}: association {side} references unknown {ref!r}'
                    )

        levels = RULE_LEVELS.get(base_id)
        if not levels:
            raise ValueError(f'{base_id}: missing RULE_LEVELS definition')
        missing = {'minimum', 'standard', 'extended'} - set(levels)
        if missing:
            raise ValueError(f'{base_id}: missing evidence levels {sorted(missing)}')
        for level_name in ('minimum', 'standard', 'extended'):
            item = levels[level_name]
            if item.get('status') in ('unavailable', 'unsupported', 'unverified'):
                continue
            level_id = item.get('rule_id')
            if not level_id:
                raise ValueError(f'{base_id}/{level_name}: missing rule_id')
            if level_id in level_ids:
                raise ValueError(f'Duplicate graded rule_id: {level_id}')
            level_ids.append(level_id)
            if 'when' not in item:
                raise ValueError(f'{base_id}/{level_name}: missing when condition')
            when = item.get('when') or {}
            level_refs = set(when.get('all_refs') or []) | set(
                when.get('any_refs') or []
            )
            unknown_refs = sorted(level_refs - known_refs)
            if unknown_refs:
                raise ValueError(
                    f'{base_id}/{level_name}: unknown evidence refs {unknown_refs}'
                )


def run(observations, ids):
    """Execute this declarative ruleset and return behavior Inferences.

    Context groups remain available through ``core.engine.run_all`` so this
    compatibility entry point preserves the profile contract's list[Inference]
    return type.
    """
    import sys

    from llmtrace.core.engine import run_all

    inferences, _ = run_all(observations, sys.modules[__name__], ids)
    return inferences


if __name__ == "__main__":
    validate_ruleset_shape()
    print(
        f"{PROFILE} ruleset {RULESET_VERSION}: "
        f"{len(INFERENCE_RULES)} inference, "
        f"{len(CONTEXT_RULES)} context, "
        f"{len(STATE_TRANSITION_RULES)} state-transition rules"
    )
