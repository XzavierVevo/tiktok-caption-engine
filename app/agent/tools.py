"""MCP-shaped tool registry wrapping existing caption-tool APIs."""

from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable

from .. import chat_models
from .. import facebook_ads
from .. import facebook_jobs
from .. import hooks
from .. import jobs
from .. import storage
from .. import tiktok_accounts
from .. import tiktok_upload
from .. import usage as usage_mod
from .. import workspace as ws
from ..encode import EncodeError, extract_frame
from ..facebook_ads import FacebookError
from ..hooks import HooksError
from ..ingest import IngestError, download_url
from ..pinterest import PinterestError, search_pins
from ..config import user_previews_dir
from . import providers

_log = logging.getLogger(__name__)

# Tools that always need a UI confirm card before execute.
CONFIRM_TOOLS = frozenset(
    {
        "add_source_url",
        "add_source_urls",
        "start_render",
        "start_tiktok_upload",
        "start_carousel_upload",
        "create_custom_sound",
        "fetch_spark",
        "start_facebook_ads",
        "delete_sources",
        "delete_outputs",
        "start_bc_warmup",
        "start_spark_bc_launch",
        "cancel_bc_job",
        "mark_bc_suspended",
        "mark_bc_disapproved",
        "mark_white_hat_approved",
        "open_bc_ads_manager",
        "start_sales_campaign",
        "continue_bc",
        "mark_bc_payment",
        "update_campaign_bid",
        "update_campaign_budget",
        "set_campaign_status",
        "approve_topup",
    }
)

# Pauses the turn for in-chat approve/deny; resume keeps tools on.
REVIEW_TOOLS = frozenset({"propose_ideas"})
# Handled specially in the agent loop (show grid / download) — not a confirm gate.
PINTEREST_SET_TOOLS = frozenset({"pinterest_set"})
MAX_REVIEW_ROUNDS = 5

TOOL_DEFS: list[dict[str, Any]] = [
    {
        "name": "list_sources",
        "description": (
            "List video sources in the user's library (chat does not auto-add them "
            "to Create). Use these sourceIds in set_render_plan / preview_plan."
        ),
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "list_images",
        "description": (
            "List still images in the library plus recent chat attachments. "
            "images[] is library insertion order. attachedInOrder is this turn's "
            "chat images. When the user said 'in order' / 'images added', chunk "
            "attachedInOrder (else images[]) sequentially into carousels."
        ),
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "get_workspace",
        "description": (
            "Get captions, style, and batch options for the current plan. "
            "In chat this returns the thread plan (source=thread); outside chat "
            "it returns Create workspace.json (source=create). Does not return "
            "named Create plans from GET /api/plans — use list_plans for chat "
            "thread plans across chats."
        ),
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "set_captions",
        "description": (
            "Replace this chat’s caption list (one hook per line). "
            "Use ONLY for matrix mode where every caption applies to every video. "
            "For N captions per video (or uneven mixes), use set_render_plan instead. "
            "Does not change Create until the user presses Send to Create on Plan. "
            "Every line must be fully readable on a phone and clear of TikTok UI."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "captions": {
                    "type": "string",
                    "description": "Full captions text, one per line",
                }
            },
            "required": ["captions"],
            "additionalProperties": False,
        },
    },
    {
        "name": "add_captions",
        "description": (
            "Append hooks to the flat workspace caption list. "
            "Prefer set_render_plan when captions belong to specific videos."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "captions": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Hooks to append, one string per caption",
                }
            },
            "required": ["captions"],
            "additionalProperties": False,
        },
    },
    {
        "name": "set_render_plan",
        "description": (
            "REQUIRED when writing captions for specific videos. "
            "Submit the full plan as structured JSON: each video’s sourceId plus "
            "its caption list. Saves the active render plan (shared across chats; "
            "same data /preview shows). Set newPlan true to add another plan instead of "
            "replacing the current one (e.g. a second batch of captions on the "
            "same videos). Do NOT paste caption lists as markdown in chat — call "
            "this tool instead. Call list_sources first to get sourceIds. Call "
            "list_plans first when the user wants captions that are not the same "
            "as an existing plan. When captions come from ask_models or /multiple, "
            "pass captionDetails [{text, model, origin: '/multiple'}] (and origin "
            "'/multiple' on the plan) so the Plan spreadsheet shows which model "
            "wrote each row."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "videos": {
                    "type": "array",
                    "description": "One entry per source video with its captions",
                    "items": {
                        "type": "object",
                        "properties": {
                            "sourceId": {
                                "type": "string",
                                "description": "Exact id from list_sources",
                            },
                            "captions": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": (
                                    "Hooks for this video only, in order. For "
                                    "mixed-model plans you may instead pass objects "
                                    "like {text, model, origin} in this array — the "
                                    "server accepts both shapes."
                                ),
                            },
                        },
                        "required": ["sourceId", "captions"],
                        "additionalProperties": False,
                    },
                },
                "origin": {
                    "type": "string",
                    "description": (
                        "How captions were produced, e.g. chat, /all, /multiple, "
                        "or pinterest"
                    ),
                },
                "model": {
                    "type": "string",
                    "description": (
                        "Default model label when captions do not set their own model"
                    ),
                },
                "captionDetails": {
                    "type": "array",
                    "description": (
                        "Optional per-caption tags matched by text. Use after "
                        "ask_models / /multiple so Plan shows which model wrote "
                        "each hook: [{text, model, origin}]."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "text": {"type": "string"},
                            "model": {"type": "string"},
                            "origin": {"type": "string"},
                            "sources": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                        },
                        "required": ["text"],
                        "additionalProperties": False,
                    },
                },
                "newPlan": {
                    "type": "boolean",
                    "description": (
                        "If true, create a new plan and leave existing "
                        "plans untouched. Use when the user asks for another plan, "
                        "another 40 captions, a second batch, etc."
                    ),
                },
                "title": {
                    "type": "string",
                    "description": "Optional label for this plan, e.g. 'Night-out hooks v2'",
                },
                "showUi": {
                    "type": "boolean",
                    "description": (
                        "If true, open the plan spreadsheet. Leave false when "
                        "continuing to start_render / upload."
                    ),
                },
            },
            "required": ["videos"],
            "additionalProperties": False,
        },
    },
    {
        "name": "set_carousel_plan",
        "description": (
            "REQUIRED for native TikTok photo carousels. Build N carousels of "
            "1–5 stills each with one TikTok description caption per carousel. "
            "When the user says images 'in order', chunk ordered image ids "
            "sequentially (e.g. 20 images → 10 two-slide carousels). "
            "Pass carousels[] with slides + caption, OR count + slideCount + "
            "imageIds + captions. Do not call start_render for carousels — "
            "use start_carousel_upload then fetch_spark."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "carousels": {
                    "type": "array",
                    "description": "Explicit carousel rows",
                    "items": {
                        "type": "object",
                        "properties": {
                            "slides": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "1–5 image library ids in slide order",
                            },
                            "caption": {"type": "string"},
                            "carouselId": {"type": "string"},
                        },
                        "required": ["slides", "caption"],
                        "additionalProperties": False,
                    },
                },
                "imageIds": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Ordered image ids to chunk when using count/slideCount",
                },
                "count": {
                    "type": "integer",
                    "description": "How many carousels to build from imageIds in order",
                },
                "slideCount": {
                    "type": "integer",
                    "description": "Slides per carousel (1–5)",
                },
                "captions": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "One caption per carousel, same order as groups",
                },
                "origin": {"type": "string"},
                "model": {"type": "string"},
                "newPlan": {"type": "boolean"},
                "title": {"type": "string"},
                "showUi": {"type": "boolean"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "list_plans",
        "description": (
            "List chat thread plans across all chats (title, counts, captions). "
            "Not the same as GET /api/plans (Create/workspace named plans). "
            "Call this before writing a new plan so captions are not duplicates."
        ),
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "ask_models",
        "description": (
            "Run the same prompt on several chat models in parallel (text only, "
            "no tools). Use when the user wants multiple models to brainstorm "
            "captions, Pinterest search angles, or hooks. Then merge the best "
            "lines into set_render_plan with per-caption {text, model} and "
            "origin '/multiple'. For Pinterest discovery: ask_models for short "
            "search topics / angles, then YOU call search_pinterest + "
            "pinterest_set (do not run Pinterest tools from each model). "
            "Render and upload use start_render / start_tiktok_upload on the "
            "merged plan (jobs may queue if others are already running)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "models": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Model ids from the catalog, e.g. claude-sonnet-5, gpt-5.4, "
                        "grok-4.6, gemini-3.6-flash. Same provider allowed."
                    ),
                },
                "prompt": {
                    "type": "string",
                    "description": "Prompt each selected model should answer",
                },
                "purpose": {
                    "type": "string",
                    "description": (
                        "Optional hint: captions | pinterest | hooks | general"
                    ),
                },
            },
            "required": ["models", "prompt"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_offers",
        "description": "List affiliate offers (hook generator).",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "get_offer",
        "description": (
            "Get one offer including winners, likes, and pastCaptions "
            "(every hook already made for this offer). Pin it as the active "
            "offer so later plan captions go into that bank."
        ),
        "parameters": {
            "type": "object",
            "properties": {"offerId": {"type": "string"}},
            "required": ["offerId"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_offer_captions",
        "description": (
            "List past on-screen captions for an offer (winners, likes, "
            "and generation/plan history). Pass this bank to Opus before "
            "writing new Reco / offer hooks."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "offerId": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["offerId"],
            "additionalProperties": False,
        },
    },
    {
        "name": "set_active_offer",
        "description": (
            "Pin the offer new chat/plan captions belong to (Reco, Freecash, …)."
        ),
        "parameters": {
            "type": "object",
            "properties": {"offerId": {"type": "string"}},
            "required": ["offerId"],
            "additionalProperties": False,
        },
    },
    {
        "name": "create_offer",
        "description": "Create an affiliate offer.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "url": {"type": "string"},
                "notes": {"type": "string"},
            },
            "required": ["name"],
            "additionalProperties": False,
        },
    },
    {
        "name": "add_winner",
        "description": "Save a winning hook on an offer.",
        "parameters": {
            "type": "object",
            "properties": {
                "offerId": {"type": "string"},
                "text": {"type": "string"},
            },
            "required": ["offerId", "text"],
            "additionalProperties": False,
        },
    },
    {
        "name": "add_like",
        "description": "Like a generated hook for an offer.",
        "parameters": {
            "type": "object",
            "properties": {
                "offerId": {"type": "string"},
                "text": {"type": "string"},
            },
            "required": ["offerId", "text"],
            "additionalProperties": False,
        },
    },
    {
        "name": "generate_hooks",
        "description": (
            "Generate affiliate hooks with Claude Opus. Reads the offer's past "
            "caption bank first. Default mode new_angles. variations/mix also ok."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "offerId": {"type": "string"},
                "count": {"type": "integer", "enum": [2, 5, 10, 20]},
                "mode": {
                    "type": "string",
                    "enum": ["variations", "mix", "new_angles"],
                    "description": "Default new_angles — fresh TikTok-ey lines.",
                },
                "extraInstructions": {"type": "string"},
                "randomWords": {
                    "type": "boolean",
                    "description": (
                        "Default false. Leave off unless asked. Reco lines say "
                        "the money, tag income with [[uncompliant:income]], and match the clip."
                    ),
                },
            },
            "required": ["offerId"],
            "additionalProperties": False,
        },
    },
    {
        "name": "set_style",
        "description": "Update caption look style settings in the workspace.",
        "parameters": {
            "type": "object",
            "properties": {"style": {"type": "object"}},
            "required": ["style"],
            "additionalProperties": False,
        },
    },
    {
        "name": "set_end_caption",
        "description": "Set end-of-video caption on a source (or apply template to all).",
        "parameters": {
            "type": "object",
            "properties": {
                "sourceId": {"type": "string"},
                "applyToAll": {"type": "boolean"},
                "enabled": {"type": "boolean"},
                "text": {"type": "string"},
                "durationSeconds": {"type": "number"},
                "xOffset": {"type": "number"},
                "yOffset": {"type": "number"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "set_batch_options",
        "description": "Set matrix/pair/custom mode, name template, skip/dedupe.",
        "parameters": {
            "type": "object",
            "properties": {
                "mode": {"type": "string", "enum": ["matrix", "pair", "custom"]},
                "nameTemplate": {"type": "string"},
                "skipExisting": {"type": "boolean"},
                "dedupe": {"type": "boolean"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "list_outputs",
        "description": "List rendered output videos in the library.",
        "parameters": {
            "type": "object",
            "properties": {"limit": {"type": "integer"}},
            "additionalProperties": False,
        },
    },
    {
        "name": "list_tiktok_accounts",
        "description": "List TikTok accounts (no cookie secrets).",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "get_batch_status",
        "description": "Poll a render batch by id.",
        "parameters": {
            "type": "object",
            "properties": {"batchId": {"type": "string"}},
            "required": ["batchId"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_upload_status",
        "description": "Poll a TikTok upload job by id.",
        "parameters": {
            "type": "object",
            "properties": {"uploadId": {"type": "string"}},
            "required": ["uploadId"],
            "additionalProperties": False,
        },
    },
    {
        "name": "preview_plan",
        "description": (
            "Build and save the active render plan (source×caption pairs; shared across chats). "
            "Pass pairs for a custom mix. Set newPlan true to add another plan "
            "instead of replacing the active one. Does not encode. "
            "By default does NOT open UI (use this when automating into start_render). "
            "Set showUi true only when the user asked to review the spreadsheet now."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "mode": {"type": "string", "enum": ["matrix", "pair", "custom"]},
                "pairs": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "sourceId": {"type": "string"},
                            "caption": {"type": "string"},
                        },
                        "required": ["sourceId", "caption"],
                    },
                },
                "newPlan": {
                    "type": "boolean",
                    "description": "If true, save as a new plan instead of replacing the active plan.",
                },
                "title": {
                    "type": "string",
                    "description": "Optional label for a new or updated plan",
                },
                "showUi": {
                    "type": "boolean",
                    "description": (
                        "If true, open the plan spreadsheet for the user to review. "
                        "Leave false when continuing to start_render / upload."
                    ),
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "search_pinterest",
        "description": (
            "Search Pinterest for pins matching a keyword. Use mediaType=image "
            "for stills (carousels) and mediaType=video for clips. Discovers pins "
            "for the model only — does NOT show the chat grid and does NOT download. "
            "After searching, you MUST call pinterest_set with action=show and the "
            "best pins (or all if few) so the user sees a selectable grid. "
            "Pass excludeUrls to skip denied or already-shown pins."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer"},
                "cursor": {"type": "string"},
                "excludeUrls": {"type": "array", "items": {"type": "string"}},
                "mediaType": {
                    "type": "string",
                    "enum": ["video", "image", "any"],
                    "description": "video (default), image for carousel stills, or any",
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "pinterest_set",
        "description": (
            "Canonical Pinterest pin-set for chat. THIS is how pins appear in the "
            "grid or get downloaded — never invent pin lists in prose.\n"
            "action=show: put pins in the selectable chat grid (user can click to "
            "attach without downloading). mode=replace (default) replaces the shown "
            "set; mode=merge adds to it.\n"
            "action=download: download pins into the library. If pins is empty, "
            "uses attached pins, then the last shown set, then the thread bank.\n"
            "Each pin needs pinUrl; copy thumbnail/title/previewUrl from "
            "search_pinterest when available."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["show", "download"]},
                "pins": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "pinUrl": {"type": "string"},
                            "title": {"type": "string"},
                            "thumbnail": {"type": "string"},
                            "previewUrl": {"type": "string"},
                            "previewKind": {"type": "string"},
                            "caption": {"type": "string"},
                            "kind": {"type": "string"},
                            "imageUrl": {"type": "string"},
                            "duration": {"type": ["number", "integer", "string"]},
                            "width": {"type": ["number", "integer"]},
                            "height": {"type": ["number", "integer"]},
                        },
                        "required": ["pinUrl"],
                        "additionalProperties": False,
                    },
                },
                "query": {"type": "string"},
                "mode": {"type": "string", "enum": ["replace", "merge"]},
            },
            "required": ["action"],
            "additionalProperties": False,
        },
    },
    {
        "name": "inspect_sources",
        "description": (
            "Extract 1–2 still frames from downloaded source videos so you can see "
            "what is on screen. Use after videos are in the library."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "sourceIds": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["sourceIds"],
            "additionalProperties": False,
        },
    },
    {
        "name": "find_carousel_formats",
        "description": (
            "Find more photo-carousel formats like a reaction creator "
            "(screen-record of TikTok Photo posts, then talking head). "
            "Pass a public Instagram/TikTok profile URL, or specific post "
            "urls[]. Downloads each video, pulls early frames, and returns "
            "them so you can read the on-screen slide text. Then remake "
            "those formats with set_carousel_plan — do not clone the original "
            "photos. Limit 1–16 (default 8)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "profileUrl": {
                    "type": "string",
                    "description": "Creator profile, e.g. https://www.instagram.com/wrd2jon/",
                },
                "urls": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Specific reel/post URLs when the profile playlist is blocked",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max posts to scan (default 8, max 16)",
                },
            },
            "additionalProperties": False,
        },
        "confirm": False,
    },
    {
        "name": "analyze_example_ad",
        "description": (
            "Analyze a public TikTok post/ad URL as a creative example: description, "
            "hashtags, cover stills (vision), optional transcript, and suggested "
            "Pinterest search seeds. Use when the user pastes a TikTok link as a "
            "reference and wants similar Pinterest images + caption variations. "
            "Does not download the video into the library (use add_source_url for that)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "Public TikTok video / ad share URL",
                },
                "getTranscript": {
                    "type": "boolean",
                    "description": "If true, also fetch spoken transcript (extra credits)",
                },
            },
            "required": ["url"],
            "additionalProperties": False,
        },
    },
    {
        "name": "propose_ideas",
        "description": (
            "Show video×caption ideas in Chat for the user to watch, approve, or deny. "
            "Use ONLY when the user wants captioned approve/deny cards (e.g. make 10 "
            "captioned ideas). For picking/showing/downloading clips alone, use "
            "pinterest_set instead. "
            "Ideas start approved; the user only has to deny pins they do not want. "
            "Does not download or render. Call AFTER search_pinterest / pinterest_set, "
            "BEFORE add_source_urls / start_render. "
            "targetCount is how many ideas the user asked for. On refill, only "
            "send the remaining slots — already-approved ideas are kept by the UI. "
            "Never ask which pins in text."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "targetCount": {"type": "integer"},
                "ideas": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "pinUrl": {"type": "string"},
                            "caption": {"type": "string"},
                            "thumbnail": {"type": "string"},
                            "title": {"type": "string"},
                            "previewUrl": {"type": "string"},
                            "previewKind": {"type": "string"},
                        },
                        "required": ["pinUrl"],
                    },
                },
            },
            "required": ["targetCount", "ideas"],
            "additionalProperties": False,
        },
    },
    {
        "name": "add_source_url",
        "description": "Download a TikTok/YouTube/Pinterest URL into the video library and current job. Requires user confirm.",
        "parameters": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
            "additionalProperties": False,
        },
        "confirm": True,
    },
    {
        "name": "add_source_urls",
        "description": "Download several TikTok/YouTube/Pinterest URLs into the library. Requires user confirm.",
        "parameters": {
            "type": "object",
            "properties": {
                "urls": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["urls"],
            "additionalProperties": False,
        },
        "confirm": True,
    },
    {
        "name": "start_render",
        "description": (
            "Start burning captions onto videos. Requires user confirm. "
            "Optionally pass pairs [{sourceId, caption}] for checked Preview tiles only. "
            "May return status queued if other renders are already running "
            "(up to 2 concurrent); keep using the returned batchId."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "pairs": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "sourceId": {"type": "string"},
                            "caption": {"type": "string"},
                        },
                        "required": ["sourceId", "caption"],
                    },
                },
                "mode": {"type": "string", "enum": ["matrix", "pair", "custom"]},
            },
            "additionalProperties": False,
        },
        "confirm": True,
    },
    {
        "name": "start_tiktok_upload",
        "description": (
            "Upload rendered videos from a finished batch to TikTok. "
            "Requires user confirm. Pass batchId from start_render. "
            "Optional accountId forces that TikTok account for every video "
            "(ignores Settings round-robin). May return status queued."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "batchId": {"type": "string"},
                "captionTemplate": {"type": "string"},
                "accountId": {
                    "type": "string",
                    "description": (
                        "Force this TikTok account id for the whole upload "
                        "(from the plan picker). Overrides Settings selection."
                    ),
                },
            },
            "required": ["batchId"],
            "additionalProperties": False,
        },
        "confirm": True,
    },
    {
        "name": "start_carousel_upload",
        "description": (
            "Publish native TikTok photo carousels from the active carousel plan "
            "(Studio, 1–5 images per post). Requires user confirm. Do not use "
            "start_render. Always returns a jobId immediately — poll upload "
            "status; `wait` is ignored. Pass soundQuery (TikTok sound search, "
            "e.g. 'if i can breathe i can think') and/or soundPath (local audio) "
            "so the carousel is not silent."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "accountId": {"type": "string"},
                "captionTemplate": {"type": "string"},
                "soundQuery": {
                    "type": "string",
                    "description": "Studio sound search, e.g. if i can breathe i can think",
                },
                "soundPath": {
                    "type": "string",
                    "description": "Local audio file to attach if search cannot pick the sound",
                },
                "useOriginalSound": {
                    "type": "boolean",
                    "description": "After get_custom_sound ready=true, attach this account's Recent original sound.",
                },
                "wait": {
                    "type": "boolean",
                    "description": "Ignored — always returns jobId immediately; poll status tools instead.",
                },
            },
            "additionalProperties": False,
        },
        "confirm": True,
    },
    {
        "name": "create_custom_sound",
        "description": (
            "Seed a TikTok original sound by posting a Studio video whose audio "
            "is the custom track (audioPath, videoPath, or sourceId). Returns "
            "uploadId immediately. Poll get_custom_sound until review is "
            "approved before start_carousel_upload / start_tiktok_upload with "
            "useOriginalSound. Requires user confirm."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "accountId": {"type": "string"},
                "audioPath": {
                    "type": "string",
                    "description": "Local audio file (mp3/m4a). Muxed onto a still if no videoPath.",
                },
                "videoPath": {
                    "type": "string",
                    "description": "Local video that already contains the custom audio.",
                },
                "sourceId": {
                    "type": "string",
                    "description": "Library source whose file already has the custom audio.",
                },
                "imagePath": {
                    "type": "string",
                    "description": "Optional still used when muxing audioPath into a seed video.",
                },
                "caption": {
                    "type": "string",
                    "description": "Seed video description (used later to find the Studio row).",
                },
            },
            "required": ["accountId"],
            "additionalProperties": False,
        },
        "confirm": True,
    },
    {
        "name": "get_custom_sound",
        "description": (
            "Poll a custom original-sound seed. Pass jobId from create_custom_sound "
            "(or accountId + query). review is uploading|pending|approved|private|"
            "rejected. Approved (ready=true) means the Studio Content row is "
            "Everyone with no 'Content under review' badge — that is the green "
            "light to start_carousel_upload / start_tiktok_upload with "
            "useOriginalSound. Only me or a review badge is not approved."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "jobId": {"type": "string"},
                "accountId": {"type": "string"},
                "query": {
                    "type": "string",
                    "description": "Seed caption to find in Studio Content if jobId is omitted.",
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "fetch_spark",
        "description": (
            "Start a DuoPlus Spark (ad authorization) job for posted plan rows. "
            "Always returns a jobId immediately — `wait` is ignored. Poll "
            "get_spark_job until codes are ready. Rows without a post URL are skipped."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "wait": {
                    "type": "boolean",
                    "description": "Ignored — always returns jobId immediately; poll get_spark_job instead.",
                },
                "pairs": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "sourceId": {"type": "string"},
                            "caption": {"type": "string"},
                        },
                        "required": ["sourceId", "caption"],
                        "additionalProperties": False,
                    },
                },
            },
            "additionalProperties": False,
        },
        "confirm": True,
    },
    {
        "name": "list_facebook_accounts",
        "description": "List connected Facebook ad accounts, Pages, and connection status.",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "list_facebook_campaigns",
        "description": "List Facebook ad campaigns on the selected ad account.",
        "parameters": {
            "type": "object",
            "properties": {"adAccountId": {"type": "string"}},
            "additionalProperties": False,
        },
    },
    {
        "name": "list_facebook_adsets",
        "description": "List Facebook ad sets, optionally filtered by campaignId.",
        "parameters": {
            "type": "object",
            "properties": {
                "campaignId": {"type": "string"},
                "adAccountId": {"type": "string"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "list_facebook_pixels",
        "description": "List Facebook pixels on the selected ad account.",
        "parameters": {
            "type": "object",
            "properties": {"adAccountId": {"type": "string"}},
            "additionalProperties": False,
        },
    },
    {
        "name": "start_facebook_ads",
        "description": (
            "Upload rendered videos to Facebook Ads as paused ads. Requires user confirm. "
            "mode: new_campaign, new_adset (needs campaignId), or existing_adset (needs adsetId). "
            "landingUrl is required. Uses Settings defaults for page, pixel, budget, and bid "
            "unless overridden."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "paths": {"type": "array", "items": {"type": "string"}},
                "batchId": {"type": "string"},
                "useLatestBatch": {"type": "boolean"},
                "mode": {
                    "type": "string",
                    "enum": ["new_campaign", "new_adset", "existing_adset"],
                },
                "landingUrl": {"type": "string"},
                "campaignId": {"type": "string"},
                "adsetId": {"type": "string"},
                "campaignName": {"type": "string"},
                "adsetName": {"type": "string"},
                "dailyBudget": {"type": "number"},
                "bidStrategy": {
                    "type": "string",
                    "enum": [
                        "LOWEST_COST_WITHOUT_CAP",
                        "LOWEST_COST_WITH_BID_CAP",
                        "none",
                        "cap",
                    ],
                },
                "bidAmount": {"type": "number"},
                "pixelId": {"type": "string"},
                "customEventType": {"type": "string"},
                "pageId": {"type": "string"},
            },
            "required": ["landingUrl"],
            "additionalProperties": False,
        },
        "confirm": True,
    },
    {
        "name": "delete_sources",
        "description": "Delete source videos by id. Requires user confirm.",
        "parameters": {
            "type": "object",
            "properties": {
                "sourceIds": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["sourceIds"],
            "additionalProperties": False,
        },
        "confirm": True,
    },
    {
        "name": "delete_outputs",
        "description": "Delete rendered output files. Requires user confirm.",
        "parameters": {
            "type": "object",
            "properties": {
                "paths": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["paths"],
            "additionalProperties": False,
        },
        "confirm": True,
    },
    {
        "name": "get_bc_settings",
        "description": (
            "Read Business Center options: day-one one-time ad-account budget "
            "(default $5), Sales Instant Page bid/budget/URL/CTAs, white-hat "
            "Spark code, extra ad accounts, add-balance amount, Mercury cap, "
            "VAT, and the last budget run (including errors)."
        ),
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "set_bc_settings",
        "description": (
            "Update Business Center options. Use adAccountBudgetUsd (or amountUsd) "
            "to change the default day-one one-time budget applied to every Ads "
            "Manager account right after adding balance. Pass sparkCode for the "
            "white-hat Reach campaign Spark paste. Pass salesConversionBidUsd, "
            "salesDailyBudgetUsd, salesLandingUrl, salesInstantPageCta, "
            "salesCreativeCta, salesDisplayCard, salesAdNameTemplate, or "
            "salesCreativeText for post-white-hat Sales Instant Page ads. "
            "salesDisplayCard attaches the TAP HERE Display Card when true. "
            "Pass accountName to override "
            "budget only for that ad account. Pass cardId / activeHostId to match "
            "Settings pickers (host change restarts the embedded worker). "
            "Does not apply budgets in TikTok immediately — it changes what the "
            "next budget-manager run will set."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "adAccountBudgetUsd": {
                    "type": "number",
                    "description": (
                        "Default one-time budget in USD for every Ads Manager "
                        "account on day one (e.g. 5, 10, 25)."
                    ),
                },
                "amountUsd": {
                    "type": "number",
                    "description": "Alias for adAccountBudgetUsd.",
                },
                "accountName": {
                    "type": "string",
                    "description": (
                        "If set, store this amount only for that Ads Manager "
                        "name (e.g. 'shop 2'). Omit to change the default."
                    ),
                },
                "sparkCode": {
                    "type": "string",
                    "description": (
                        "White-hat Spark code pasted into Ads Manager "
                        "(e.g. #0QeVFz…). Alias: sparkCodeDefault."
                    ),
                },
                "sparkCodeDefault": {
                    "type": "string",
                    "description": "Alias for sparkCode.",
                },
                "extraAdAccounts": {
                    "type": "integer",
                    "description": (
                        "Settings default extras beyond the seed account (0–20). "
                        "For a launch-specific override, pass accountsPerBc on "
                        "start_spark_bc_launch or start_bc_warmup instead."
                    ),
                },
                "addBalanceAmount": {
                    "type": "number",
                    "description": "Add-balance top-up in USD (minimum 10).",
                },
                "mercuryDailyLimit": {
                    "type": "number",
                    "description": "Mercury daily spend cap in USD.",
                },
                "vatId": {
                    "type": "string",
                    "description": "VAT catalog id (e.g. se_556517).",
                },
                "cardId": {
                    "type": "string",
                    "description": "Saved billing card id from Settings → Business Center.",
                },
                "activeHostId": {
                    "type": "string",
                    "description": (
                        "AdsPower host id to make active. Restarts the embedded "
                        "provision worker so the new URL/key take effect."
                    ),
                },
                "salesConversionBidUsd": {
                    "type": "number",
                    "description": "Sales Instant Page conversion (CPA) bid in USD.",
                },
                "salesDailyBudgetUsd": {
                    "type": "number",
                    "description": "Sales ad-group daily budget in USD.",
                },
                "salesLandingUrl": {
                    "type": "string",
                    "description": "Landing URL override (blank uses the offer URL).",
                },
                "salesInstantPageCta": {
                    "type": "string",
                    "description": "Instant Page button text (default Continue).",
                },
                "salesCreativeCta": {
                    "type": "string",
                    "description": "Creative CTA (default Learn more).",
                },
                "salesDisplayCard": {
                    "type": "boolean",
                    "description": (
                        "Attach the TAP HERE Display Card to Sales ads. "
                        "True by default; false launches without a card."
                    ),
                },
                "salesAdNameTemplate": {
                    "type": "string",
                    "description": "Ad name template, {offer} and {i} placeholders.",
                },
                "salesCreativeText": {
                    "type": "string",
                    "description": "Optional ad text on each Sales creative.",
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "list_bcs",
        "description": (
            "List Business Center inventory profiles with status, white-hat / "
            "ads-ready flags, payment-required, suspension, health, notes, and "
            "ad-account summaries. Optional view filter: warming_up, ads_ready, "
            "white_hat_pending, payment_required, disapproved, suspended, failed, "
            "needs_manual_login."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "view": {
                    "type": "string",
                    "description": "Saved-view filter (optional).",
                },
                "query": {
                    "type": "string",
                    "description": "Search email, BC name, notes, account id.",
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "get_bc",
        "description": (
            "Get one BC profile with ad accounts, white-hat details, spark pack "
            "for intended offers, notes, health, and events."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "profileId": {"type": "string"},
            },
            "required": ["profileId"],
            "additionalProperties": False,
        },
    },
    {
        "name": "preview_spark_bc_launch",
        "description": (
            "Preview how many Business Centers / AdsPower profiles a Spark pack "
            "needs (10 sparks per ad account). Pass sparkCodes and/or "
            "intendedOfferIds plus optional accountsPerBc / bcCount. Returns "
            "capacity math, exclusive slices, and Mercury spend preview. "
            "Does not start a job."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "intendedOfferIds": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "sparkCodes": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Pasted Spark codes (overrides offer catalog).",
                },
                "sparkText": {
                    "type": "string",
                    "description": "Paste blob of Spark codes (whitespace-separated).",
                },
                "accountsPerBc": {
                    "type": "integer",
                    "description": "Ad accounts per BC (default 1). 2 fits 20 sparks in one profile.",
                },
                "extraAdAccounts": {
                    "type": "integer",
                    "description": "Alias for accountsPerBc-1 (extras beyond the seed).",
                },
                "bcCount": {
                    "type": "integer",
                    "description": "Override BC count (must be ≥ ceil(sparks / capacity)).",
                },
                "geo": {"type": "string"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "start_bc_warmup",
        "description": (
            "Warm up exactly bcCount Business Centers (default 1). A retry while "
            "that warmup is still running returns the same job and does not create "
            "another BC. Each BC gets a profile, billing, and every ad account from "
            "Settings (seed plus extraAdAccounts), then white hat on each account. "
            "Does not auto-launch Sales. Requires confirm."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "bcCount": {
                    "type": "integer",
                    "description": "Number of Business Centers / AdsPower profiles (default 1).",
                },
                "count": {
                    "type": "integer",
                    "description": "Alias for bcCount.",
                },
                "geo": {"type": "string", "description": "Geo code (default SE)."},
                "reason": {"type": "string"},
                "intendedOfferIds": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Offer ids to label these BCs (optional).",
                },
                "accountsPerBc": {
                    "type": "integer",
                    "description": "Ad accounts inside each BC. Omit this and extraAdAccounts to use Settings.",
                },
                "extraAdAccounts": {
                    "type": "integer",
                    "description": "Extras beyond the seed for this job. Omit to use Settings.",
                },
            },
            "additionalProperties": False,
        },
        "confirm": True,
    },
    {
        "name": "start_spark_bc_launch",
        "description": (
            "Launch BCs sized from Spark count (10 per ad account) and assign "
            "exclusive Spark packs so Sales Instant Page ads run automatically "
            "after white-hat Scheduled. 20 sparks + 1 account → 2 BCs / 2 profiles; "
            "20 sparks + 2 accounts → 1 BC. Requires confirm. Worker must be online."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "intendedOfferIds": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "sparkCodes": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "sparkText": {"type": "string"},
                "accountsPerBc": {"type": "integer"},
                "extraAdAccounts": {"type": "integer"},
                "bcCount": {
                    "type": "integer",
                    "description": "Override (must be ≥ minimum from capacity math).",
                },
                "geo": {"type": "string"},
                "reason": {"type": "string"},
            },
            "additionalProperties": False,
        },
        "confirm": True,
    },
    {
        "name": "get_bc_job",
        "description": "Poll a provision / BC warm-up job by id.",
        "parameters": {
            "type": "object",
            "properties": {"jobId": {"type": "string"}},
            "required": ["jobId"],
            "additionalProperties": False,
        },
    },
    {
        "name": "cancel_bc_job",
        "description": "Stop a running or queued BC warm-up job in place so it can be Continued from the BC table. Does not tear down the profile/proxy/Hoot. Requires confirm.",
        "parameters": {
            "type": "object",
            "properties": {"jobId": {"type": "string"}},
            "required": ["jobId"],
            "additionalProperties": False,
        },
        "confirm": True,
    },
    {
        "name": "mark_bc_suspended",
        "description": (
            "Manually mark a BC profile or ad account as suspended. "
            "reasonCode: policy | payment | identity | unknown. Requires confirm. "
            "Do NOT use for Business Center disapproval / failed review — use "
            "mark_bc_disapproved instead."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "profileId": {"type": "string"},
                "scope": {
                    "type": "string",
                    "enum": ["profile", "account"],
                },
                "accountId": {"type": "string"},
                "accountName": {"type": "string"},
                "reasonCode": {
                    "type": "string",
                    "enum": ["policy", "payment", "identity", "unknown"],
                },
                "reason": {"type": "string"},
            },
            "required": ["profileId"],
            "additionalProperties": False,
        },
        "confirm": True,
    },
    {
        "name": "mark_bc_disapproved",
        "description": (
            "Mark a BC profile as disapproved (failed review) — distinct from "
            "suspended. Burns the assigned proxy (unusable) and bans the Hoot "
            "account for the refund workflow. Requires confirm."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "profileId": {"type": "string"},
                "reason": {"type": "string"},
                "processRefund": {
                    "type": "boolean",
                    "description": "Default true: mark proxy unusable + Hoot banned.",
                },
            },
            "required": ["profileId"],
            "additionalProperties": False,
        },
        "confirm": True,
    },
    {
        "name": "mark_white_hat_approved",
        "description": (
            "Manually mark an ad account white-hat as Scheduled/approved "
            "(when verified in Ads Manager but watcher missed it). Requires confirm."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "profileId": {"type": "string"},
                "accountId": {"type": "string"},
                "accountName": {"type": "string"},
            },
            "required": ["profileId"],
            "additionalProperties": False,
        },
        "confirm": True,
    },
    {
        "name": "continue_bc",
        "description": (
            "Resume an existing Business Center from its current incomplete step "
            "(same as the BCs Continue button). Does not create a new profile, "
            "proxy, or Hoot account. Requires confirm. Worker must be online."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "profileId": {"type": "string"},
            },
            "required": ["profileId"],
            "additionalProperties": False,
        },
        "confirm": True,
    },
    {
        "name": "mark_bc_payment",
        "description": (
            "Mark payment as already added on a Business Center (same as "
            "BCs → Mark payment added). Skips the card charge. amountUsd "
            "defaults to 10. Requires confirm."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "profileId": {"type": "string"},
                "amountUsd": {
                    "type": "number",
                    "description": "Amount recorded. Default 10.",
                },
            },
            "required": ["profileId"],
            "additionalProperties": False,
        },
        "confirm": True,
    },
    {
        "name": "list_sparks",
        "description": "List loaded TikTok posts and their spend across campaigns and ad accounts.",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "get_tiktok_connection",
        "description": "Marketing API connection status for a BC profile. Never returns the token.",
        "parameters": {
            "type": "object",
            "properties": {"profileId": {"type": "string"}},
            "required": ["profileId"],
            "additionalProperties": False,
        },
    },
    {
        "name": "sync_tiktok_insights",
        "description": "Sync TikTok spend, impressions, and cost per conversion for connected advertisers.",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "list_topups",
        "description": "List ad-account top-up tasks.",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "update_campaign_bid",
        "description": "Change the bid on a campaign's ad group. Requires confirm.",
        "confirm": True,
        "parameters": {
            "type": "object",
            "properties": {
                "profileId": {"type": "string"},
                "advertiserId": {"type": "string"},
                "adgroupId": {"type": "string"},
                "bid": {"type": "number"},
                "lead": {"type": "boolean"},
            },
            "required": ["profileId", "advertiserId", "adgroupId", "bid"],
            "additionalProperties": False,
        },
    },
    {
        "name": "update_campaign_budget",
        "description": "Change the daily budget on a campaign's ad group. Requires confirm.",
        "confirm": True,
        "parameters": {
            "type": "object",
            "properties": {
                "profileId": {"type": "string"},
                "advertiserId": {"type": "string"},
                "adgroupId": {"type": "string"},
                "budget": {"type": "number"},
            },
            "required": ["profileId", "advertiserId", "adgroupId", "budget"],
            "additionalProperties": False,
        },
    },
    {
        "name": "set_campaign_status",
        "description": "Pause or enable a campaign. Requires confirm.",
        "confirm": True,
        "parameters": {
            "type": "object",
            "properties": {
                "profileId": {"type": "string"},
                "advertiserId": {"type": "string"},
                "campaignId": {"type": "string"},
                "status": {"type": "string"},
            },
            "required": ["profileId", "advertiserId", "campaignId", "status"],
            "additionalProperties": False,
        },
    },
    {
        "name": "approve_topup",
        "description": "Approve a manual top-up so the PC worker can charge the card. Requires confirm.",
        "confirm": True,
        "parameters": {
            "type": "object",
            "properties": {"topupId": {"type": "string"}},
            "required": ["topupId"],
            "additionalProperties": False,
        },
    },
    {
        "name": "start_sales_campaign",
        "description": (
            "Launch one Sales or Lead campaign on an ad account through the "
            "Marketing API. Same as BCs → Load sparks. White-hat posted is enough; "
            "do not wait for Scheduled and do not require Spark codes. Pass "
            "profileId, optional account (1-based slot, name, or advertiser id), "
            "conversionBidUsd, dailyBudgetUsd, objective (sales or lead), "
            "landingUrl, and offerIds. sparkCodes only when the user pasted them. "
            "Set autoPick=true to choose the first available account. "
            "Requires confirm. Worker must be online. Campaign and ads are "
            "published active."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "profileId": {
                    "type": "string",
                    "description": "BC profile id. Optional when autoPick is true.",
                },
                "sparkCodes": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional Spark codes; otherwise the assigned pack.",
                },
                "landingUrl": {
                    "type": "string",
                    "description": "Instant Page website URL (else offer URL / Settings).",
                },
                "offerIds": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Offer ids to attach; updates profile intendedOfferIds.",
                },
                "account": {
                    "type": "string",
                    "description": (
                        "Ad account: 1-based slot, account name, or TikTok "
                        "advertiser id. Default 1 (seed)."
                    ),
                },
                "force": {
                    "type": "boolean",
                    "description": "Re-enqueue even if Sales already draft/done.",
                },
                "autoPick": {
                    "type": "boolean",
                    "description": (
                        "Pick the first available ads-ready ad account when "
                        "profileId is omitted or empty."
                    ),
                },
                "objective": {"type": "string", "description": "sales or lead. Default sales."},
                "conversionBidUsd": {"type": "number"},
                "dailyBudgetUsd": {"type": "number"},
                "bidScope": {
                    "type": "string",
                    "description": (
                        "website-ios = Website destination, conversion bid, "
                        "dummy Pixel, iOS only. Instant Page is CONVERT BUTTON."
                    ),
                },
                "creatorAccountId": {"type": "string"},
                "itemIds": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "TikTok post item ids to load as ads.",
                },
                "postUrl": {
                    "type": "string",
                    "description": "One TikTok photo or video URL. A /photo/ URL is a carousel ad.",
                },
                "postUrls": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "TikTok /photo/ and /video/ URLs in one launch. "
                        "Each photo is CAROUSEL_ADS and each video is SINGLE_VIDEO."
                    ),
                },
                "posts": {
                    "type": "array",
                    "description": (
                        "One row per ad. adFormat is CAROUSEL_ADS or SINGLE_VIDEO. "
                        "Each row keeps its own format, name, and item id."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "itemId": {"type": "string"},
                            "adFormat": {"type": "string"},
                            "postUrl": {"type": "string"},
                        },
                    },
                },
                "topupAmountUsd": {"type": "number"},
                "topupThresholdUsd": {"type": "number"},
                "pageId": {"type": "string"},
                "advertiserId": {"type": "string"},
            },
            "required": [],
            "additionalProperties": False,
        },
        "confirm": True,
    },
    {
        "name": "ensure_dummy_pixel",
        "description": (
            "Reuse a Pixel already on the Business Center, or create website-dummy, "
            "and link it to the ad account. This is only so a Sales ad can use a "
            "website link. It does not create an Instant Page."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "profileId": {
                    "type": "string",
                    "description": "BC profile id.",
                },
                "account": {
                    "type": "string",
                    "description": "Ad account: 1-based slot, account name, or TikTok advertiser id. Default 1.",
                },
                "advertiserId": {
                    "type": "string",
                    "description": "TikTok advertiser id. Overrides account when set.",
                },
            },
            "required": ["profileId"],
            "additionalProperties": False,
        },
        "confirm": False,
    },
    {
        "name": "set_bc_notes",
        "description": "Set free-text notes on a BC profile (e.g. use for DE Freecash).",
        "parameters": {
            "type": "object",
            "properties": {
                "profileId": {"type": "string"},
                "notes": {"type": "string"},
            },
            "required": ["profileId", "notes"],
            "additionalProperties": False,
        },
    },
    {
        "name": "open_bc_ads_manager",
        "description": (
            "Enqueue opening Ads Manager for an ad account on the provision "
            "worker machine. Requires confirm. Worker must be online."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "profileId": {"type": "string"},
                "accountRef": {
                    "type": "string",
                    "description": "Account name, id, or 1-based index (default 1).",
                },
            },
            "required": ["profileId"],
            "additionalProperties": False,
        },
        "confirm": True,
    },
]


# Sending every integration's schema on every caption request costs a large
# amount of context before the user has said anything.  Keep the common
# caption workflow immediately available and opt into specialist tools from
# the current request (or from tools already used in this conversation).
CORE_CHAT_TOOLS = frozenset({
    "list_sources", "list_images", "get_workspace", "set_captions",
    "add_captions", "set_render_plan", "list_plans", "list_offers",
    "get_offer", "list_offer_captions", "set_active_offer", "create_offer",
    "add_winner", "add_like", "generate_hooks",
    "set_style", "set_end_caption", "set_batch_options", "list_outputs",
    "get_batch_status", "preview_plan", "inspect_sources", "propose_ideas",
    "add_source_url", "add_source_urls", "delete_sources", "delete_outputs",
    "start_render",
})

CHAT_TOOL_GROUPS: dict[str, frozenset[str]] = {
    "pinterest": frozenset({"search_pinterest", "pinterest_set", "analyze_example_ad", "find_carousel_formats"}),
    "carousel": frozenset({"set_carousel_plan", "start_carousel_upload", "find_carousel_formats"}),
    "publish": frozenset({"list_tiktok_accounts", "get_upload_status", "start_tiktok_upload", "fetch_spark", "create_custom_sound", "get_custom_sound"}),
    "meta": frozenset({"list_facebook_accounts", "list_facebook_campaigns", "list_facebook_adsets", "list_facebook_pixels", "start_facebook_ads"}),
    "business_center": frozenset({"get_bc_settings", "set_bc_settings", "list_bcs", "get_bc", "preview_spark_bc_launch", "start_bc_warmup", "start_spark_bc_launch", "get_bc_job", "cancel_bc_job", "mark_bc_suspended", "mark_bc_disapproved", "mark_white_hat_approved", "continue_bc", "mark_bc_payment", "start_sales_campaign", "ensure_dummy_pixel", "set_bc_notes", "open_bc_ads_manager", "list_sparks", "get_tiktok_connection", "sync_tiktok_insights", "list_topups", "update_campaign_bid", "update_campaign_budget", "set_campaign_status", "approve_topup"}),
}

# Campaign/launch language, plus a bare profile number (264). Short words use
# boundaries so "forbid" or "10 captions" do not open the BC tool set.
_BC_QUERY = re.compile(
    r"\b(?:business\s+centers?|bcs?|warm[\s-]?ups?|warmup|adspower|"
    r"white[\s-]?hats?|whitehat|sales\s+campaigns?|launches|launch(?:ing|ed)?|"
    r"campaigns?|ad\s+accounts?|profiles?|bids?|budgets?|top[\s-]?ups?)\b"
    r"|\b\d{3}\b",
    re.IGNORECASE,
)


def chat_tool_names(thread: dict | None) -> frozenset[str]:
    """Return only tools relevant to the latest chat request and active workflow."""
    messages = (thread or {}).get("messages") or []
    recent = messages[-12:]
    text = "\n".join(
        str(m.get("content") or "") for m in recent if m.get("role") == "user"
    ).lower()
    used = {
        str(call.get("name") or "")
        for m in recent if isinstance(m, dict)
        for call in (m.get("tool_calls") or []) if isinstance(call, dict)
    }
    selected = set(CORE_CHAT_TOOLS)
    for group, names in CHAT_TOOL_GROUPS.items():
        if used & names:
            selected.update(names)
    if any(word in text for word in ("pinterest", " pin", "pins ", "similar creative", "similar image", "example ad")):
        selected.update(CHAT_TOOL_GROUPS["pinterest"])
    if any(
        word in text
        for word in (
            "carousel",
            "carousels",
            "photo post",
            "slides",
            "slide ",
            "wrd2jon",
            "carousel format",
            "instagram.com/",
        )
    ):
        selected.update(CHAT_TOOL_GROUPS["carousel"])
    if any(
        word in text
        for word in (
            "upload",
            "post to tiktok",
            "publish",
            "spark code",
            "spark codes",
            "custom sound",
            "original sound",
        )
    ):
        selected.update(CHAT_TOOL_GROUPS["publish"])
    if any(word in text for word in ("facebook", "meta ads", "ad set", "ads manager", "pixel")):
        selected.update(CHAT_TOOL_GROUPS["meta"])
    if _BC_QUERY.search(text):
        selected.update(CHAT_TOOL_GROUPS["business_center"])
        if any(word in text for word in ("upload", "render")):
            selected.update(CHAT_TOOL_GROUPS["publish"])
    return frozenset(selected)


def _selected_defs(names: frozenset[str] | None = None) -> list[dict[str, Any]]:
    return [t for t in TOOL_DEFS if names is None or t["name"] in names]


def openai_tools(names: frozenset[str] | None = None) -> list[dict]:
    """OpenAI / xAI function-calling schema."""
    out = []
    for t in _selected_defs(names):
        out.append(
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t["description"],
                    "parameters": t["parameters"],
                },
            }
        )
    return out


def anthropic_tools(
    names: frozenset[str] | None = None, *, cache_prefix: bool = False
) -> list[dict]:
    """Anthropic schemas, optionally ending the stable tool prefix in a cache."""
    out = []
    for t in _selected_defs(names):
        out.append(
            {
                "name": t["name"],
                "description": t["description"],
                "input_schema": t["parameters"],
            }
        )
    # Anthropic caches the system prompt and all earlier tools at the last
    # cache_control breakpoint.  The selected tool list has deterministic
    # TOOL_DEFS order, so it remains a reusable prefix within a workflow.
    if cache_prefix and out:
        out[-1]["cache_control"] = {"type": "ephemeral"}
    return out


def _gemini_schema(node: Any) -> Any:
    """Gemini FunctionDeclaration Schema rejects oneOf/anyOf/additionalProperties."""
    if isinstance(node, list):
        return [_gemini_schema(x) for x in node]
    if not isinstance(node, dict):
        return node
    alts = node.get("oneOf") or node.get("anyOf")
    if alts:
        dicts = [a for a in alts if isinstance(a, dict)]
        picked = next((a for a in dicts if a.get("type") == "array"), None)
        if picked is None:
            picked = dicts[0] if dicts else {"type": "string"}
        merged = dict(picked)
        if node.get("description"):
            merged.setdefault("description", node["description"])
        return _gemini_schema(merged)
    out: dict[str, Any] = {}
    for k, v in node.items():
        if k in (
            "additionalProperties",
            "oneOf",
            "anyOf",
            "allOf",
            "$schema",
            "$ref",
            "default",
        ):
            continue
        if k == "properties" and isinstance(v, dict):
            out[k] = {pk: _gemini_schema(pv) for pk, pv in v.items()}
        elif k == "enum":
            if isinstance(v, list) and all(isinstance(x, str) for x in v):
                out[k] = v
            # Gemini Schema.enum is list[str] only; drop numeric enums.
        elif k == "items":
            out[k] = _gemini_schema(v)
        else:
            out[k] = v
    return out


def gemini_tools(names: frozenset[str] | None = None) -> list[dict]:
    # google-genai FunctionDeclaration-compatible dicts
    out = []
    for t in _selected_defs(names):
        out.append(
            {
                "name": t["name"],
                "description": t["description"],
                "parameters": _gemini_schema(t["parameters"]),
            }
        )
    return out


def mcp_tool_list() -> list[dict]:
    return [
        {
            "name": t["name"],
            "description": t["description"],
            "inputSchema": t["parameters"],
        }
        for t in TOOL_DEFS
    ]


class ToolContext:
    """Callbacks into main.py state (sources registry, etc.)."""

    def __init__(
        self,
        *,
        list_sources: Callable[[str], list[dict]],
        delete_source: Callable[[str, str], None],
        register_downloaded: Callable[..., dict],
        set_end_caption: Callable[[str, str, dict], dict],
        list_outputs: Callable[[str, int], dict],
        delete_outputs: Callable[[str, list[str]], dict],
        build_batch_videos: Callable[[str, list[str] | None, dict | None], list[dict]],
        start_batch_from_pairs: Callable[..., Any],
        set_source_crop: Callable[[str, str, dict | None], dict] | None = None,
        list_library: Callable[[str], list[dict]] | None = None,
        thread_id: str = "",
    ) -> None:
        self.list_sources = list_sources
        self.delete_source = delete_source
        self.register_downloaded = register_downloaded
        self.set_end_caption = set_end_caption
        self.list_outputs = list_outputs
        self.delete_outputs = delete_outputs
        self.build_batch_videos = build_batch_videos
        self.start_batch_from_pairs = start_batch_from_pairs
        self.set_source_crop = set_source_crop
        self.list_library = list_library
        self.thread_id = thread_id or ""
        self.last_pinterest_pins: list[dict] = []


def normalize_pin(raw: dict | None) -> dict | None:
    """Canonical pin dict for bank / grid / download."""
    if not isinstance(raw, dict):
        return None
    url = (raw.get("pinUrl") or raw.get("url") or "").strip()
    if not url:
        return None
    title = str(raw.get("title") or raw.get("name") or "").strip()
    caption = str(raw.get("caption") or title or "").strip()
    return {
        "pinUrl": url,
        "url": url,
        "title": title,
        "caption": caption,
        "thumbnail": (raw.get("thumbnail") or raw.get("thumbUrl") or "").strip(),
        "previewUrl": (raw.get("previewUrl") or "").strip(),
        "previewKind": (raw.get("previewKind") or "").strip(),
        "kind": str(raw.get("kind") or "").strip()
        or ("image" if (raw.get("previewKind") or "") == "image" else "video"),
        "imageUrl": (raw.get("imageUrl") or "").strip(),
        "duration": raw.get("duration") or 0,
        "width": raw.get("width") or 0,
        "height": raw.get("height") or 0,
        "pinId": raw.get("pinId") or "",
    }


def normalize_pins(raw_list: list | None) -> list[dict]:
    by_url: dict[str, dict] = {}
    for raw in raw_list or []:
        pin = normalize_pin(raw if isinstance(raw, dict) else None)
        if not pin:
            continue
        key = pin["pinUrl"].rstrip("/").lower()
        by_url[key] = pin
    return list(by_url.values())


def _caption_meta_index(workspace: dict) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for row in workspace.get("captionMeta") or []:
        if not isinstance(row, dict):
            continue
        text = str(row.get("text") or "").strip()
        if text and text not in out:
            out[text] = row
    return out


def _load_plan(uid: str, ctx: ToolContext) -> dict:
    """Plan fields for preview/render — thread-scoped when in chat."""
    if ctx.thread_id:
        from .. import chat_store

        return chat_store.get_thread_plan(uid, ctx.thread_id)
    return ws.load_workspace(uid)


def _save_plan(
    uid: str,
    ctx: ToolContext,
    patch: dict[str, Any],
    *,
    new_plan: bool = False,
    title: str = "",
) -> dict:
    if ctx.thread_id:
        from .. import chat_store

        return chat_store.save_thread_plan(
            uid, ctx.thread_id, patch, new_plan=new_plan, title=title
        )
    return ws.save_workspace(uid, patch)


def _record_hooks(
    uid: str,
    ctx: ToolContext,
    hooks: list[str],
    *,
    extra: str = "",
) -> None:
    if not hooks:
        return
    try:
        from .. import hooks as hooks_mod

        oid = hooks_mod.resolve_offer_id(uid)
        hooks_mod.record_generated_hooks(
            uid,
            hooks,
            offer_id=oid,
            mode="chat",
            extra=extra,
            thread_id=ctx.thread_id or "",
        )
    except Exception:
        _log.debug("Hook history record failed", exc_info=True)


def _ask_models(
    uid: str,
    ctx: ToolContext,
    args: dict | None = None,
) -> dict[str, Any]:
    """Fan a text prompt out to several models in parallel (no tools)."""
    args = args or {}
    prompt = str(args.get("prompt") or "").strip()
    if not prompt:
        raise ValueError("prompt is required")
    raw_ids = args.get("models")
    if not isinstance(raw_ids, list) or not raw_ids:
        raise ValueError("models must be a non-empty array of model ids")
    models = chat_models.resolve_models([str(x) for x in raw_ids])
    if not models:
        raise ValueError(
            "No usable models from that list. Check ids and provider API keys."
        )
    purpose = str(args.get("purpose") or "").strip().lower()
    system = None
    if purpose in ("captions", "hooks"):
        system = (
            "Write finished on-screen captions only. Numbered list. "
            "Follow the user prompt. Do not add extra rules."
        )
    elif purpose == "pinterest":
        system = (
            "You suggest short Pinterest video search topics and angles for "
            "TikTok affiliates. Reply with a numbered list of short keyword "
            "queries (2–6 words each). No URLs."
        )
    elif not purpose or purpose == "general":
        # Default to caption rules when the prompt is clearly asking for hooks.
        from . import caption_pick as cap_pick

        if cap_pick.looks_like_caption_prompt(prompt):
            system = (
                providers.CAPTION_WRITING_RULES
                + "\n\nReply with a numbered list of finished on-screen caption "
                "lines only — no preamble, no angle labels."
            )

    messages = [{"role": "user", "content": prompt}]

    def _one(model: dict) -> dict[str, Any]:
        try:
            result = providers.chat_completion(
                provider=model["provider"],
                model=model["id"],
                messages=messages,
                allow_tools=False,
                system=system if system is not None else "",
            )
            try:
                usage_mod.record_llm(
                    uid,
                    thread_id=ctx.thread_id or "",
                    provider=model["provider"],
                    model=model["id"],
                    usage=(result or {}).get("usage"),
                    kind="ask_models",
                )
            except Exception:
                _log.debug("ask_models usage record failed", exc_info=True)
            content = (result.get("content") or "").strip()
            if not content:
                raise ValueError("Empty reply from model")
            return {
                "provider": model["provider"],
                "model": model["id"],
                "label": model.get("label") or model["id"],
                "content": content,
                "error": None,
            }
        except Exception as exc:
            _log.exception("ask_models failed for %s", model.get("id"))
            return {
                "provider": model["provider"],
                "model": model["id"],
                "label": model.get("label") or model["id"],
                "content": "",
                "error": f"{type(exc).__name__}: {exc}",
            }

    bots: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max(len(models), 1)) as pool:
        futs = {pool.submit(_one, m): m for m in models}
        by_id: dict[str, dict] = {}
        for fut in as_completed(futs):
            bot = fut.result()
            by_id[bot["model"]] = bot
        bots = [by_id[m["id"]] for m in models if m["id"] in by_id]

    ok_n = sum(1 for b in bots if not b.get("error"))
    return {
        "ok": True,
        "purpose": purpose or "general",
        "count": len(bots),
        "okCount": ok_n,
        "bots": bots,
        "hint": (
            "Merge useful lines into set_render_plan: captions as strings plus "
            "captionDetails [{text, model: label, origin: '/multiple'}]. "
            "For Pinterest angles, call search_pinterest yourself with short "
            "topics — do not invent pins."
        ),
    }


def _pair_origin_fields(
    pair: dict,
    *,
    mode: str,
    meta_by_cap: dict[str, dict],
) -> dict[str, Any]:
    cap = str(pair.get("caption") or "").strip()
    meta = meta_by_cap.get(cap) or {}
    sources = pair.get("sources") or meta.get("sources") or []
    if not isinstance(sources, list):
        sources = [sources] if sources else []
    sources = [str(s).strip() for s in sources if str(s).strip()]
    origin = str(pair.get("origin") or meta.get("origin") or "").strip()
    model = str(pair.get("model") or meta.get("model") or "").strip()
    if not origin:
        if sources:
            origin = "/all" if len(sources) > 1 else "chat"
        else:
            origin = mode
    if not model and sources:
        model = " · ".join(sources)
    row = dict(pair)
    row["origin"] = origin
    if model:
        row["model"] = model
    if sources:
        row["sources"] = sources
    return row


def _normalize_caption_entries(
    caps_raw: Any,
    *,
    default_origin: str,
    default_model: str,
) -> list[dict[str, Any]]:
    """Turn captions[] (strings or objects) into {text, model, origin, sources}."""
    if isinstance(caps_raw, str):
        caps_list: list[Any] = [caps_raw]
    elif isinstance(caps_raw, list):
        caps_list = caps_raw
    else:
        caps_list = []
    out: list[dict[str, Any]] = []
    for item in caps_list:
        if isinstance(item, dict):
            text = str(item.get("text") or item.get("caption") or "").strip()
            if not text:
                continue
            model = str(item.get("model") or default_model or "").strip()
            origin = str(item.get("origin") or default_origin or "chat").strip() or "chat"
            sources = item.get("sources") or ([model] if model else [])
            if not isinstance(sources, list):
                sources = [sources] if sources else []
            sources = [str(s).strip() for s in sources if str(s).strip()]
            row: dict[str, Any] = {"text": text, "origin": origin}
            if model:
                row["model"] = model
            if sources:
                row["sources"] = sources
            out.append(row)
            continue
        text = str(item or "").strip()
        if not text:
            continue
        row = {"text": text, "origin": default_origin or "chat"}
        if default_model:
            row["model"] = default_model
            row["sources"] = [default_model]
        out.append(row)
    return out


def apply_render_plan(
    uid: str,
    ctx: ToolContext,
    args: dict | None = None,
) -> dict[str, Any]:
    """Save a structured per-video caption plan as renderPairs (JSON in, plan out)."""
    args = args or {}
    raw_videos = args.get("videos")
    if not isinstance(raw_videos, list) or not raw_videos:
        raise ValueError("videos must be a non-empty array of {sourceId, captions[]}")

    srcs = ctx.list_sources(uid)
    by_id = {s["id"]: s for s in srcs}
    if not by_id:
        raise ValueError("No source videos — add or download clips first (list_sources)")

    origin = str(args.get("origin") or "chat").strip() or "chat"
    model = str(args.get("model") or "").strip()
    detail_by_cap: dict[str, dict[str, Any]] = {}
    for row in args.get("captionDetails") or []:
        if not isinstance(row, dict):
            continue
        text = str(row.get("text") or row.get("caption") or "").strip()
        if not text or text in detail_by_cap:
            continue
        detail_by_cap[text] = row
    # When captionDetails carry per-line model tags, do not stamp every row with the
    # combined /all model string passed at the top level.
    default_model = "" if detail_by_cap else model
    pairs: list[dict[str, Any]] = []
    caption_meta: list[dict[str, Any]] = []
    seen_caps: set[str] = set()
    groups_out: list[dict[str, Any]] = []
    unknown: list[str] = []

    for item in raw_videos:
        if not isinstance(item, dict):
            continue
        sid = str(item.get("sourceId") or "").strip()
        if not sid:
            continue
        if sid not in by_id:
            unknown.append(sid)
            continue
        entries = _normalize_caption_entries(
            item.get("captions"),
            default_origin=origin,
            default_model=default_model,
        )
        # Overlay captionDetails when the video list used plain strings.
        for entry in entries:
            detail = detail_by_cap.get(entry["text"])
            if not detail:
                continue
            if detail.get("model"):
                entry["model"] = str(detail.get("model") or "").strip()
            if detail.get("origin"):
                entry["origin"] = str(detail.get("origin") or "").strip() or entry["origin"]
            srcs = detail.get("sources")
            if isinstance(srcs, list) and srcs:
                entry["sources"] = [str(s).strip() for s in srcs if str(s).strip()]
            elif entry.get("model") and not entry.get("sources"):
                entry["sources"] = [entry["model"]]
        if not entries:
            continue
        src = by_id[sid]
        name = src.get("name") or sid
        cleaned = [e["text"] for e in entries]
        groups_out.append(
            {
                "sourceId": sid,
                "sourceName": name,
                "captionCount": len(cleaned),
                "captions": cleaned,
            }
        )
        for entry in entries:
            cap = entry["text"]
            row: dict[str, Any] = {
                "sourceId": sid,
                "sourceName": name,
                "caption": cap,
                "origin": entry.get("origin") or origin,
            }
            if entry.get("model"):
                row["model"] = entry["model"]
            if entry.get("sources"):
                row["sources"] = list(entry["sources"])
            pairs.append(row)
            if cap not in seen_caps:
                seen_caps.add(cap)
                meta: dict[str, Any] = {
                    "text": cap,
                    "origin": entry.get("origin") or origin,
                }
                if entry.get("model"):
                    meta["model"] = entry["model"]
                if entry.get("sources"):
                    meta["sources"] = list(entry["sources"])
                elif entry.get("model"):
                    meta["sources"] = [entry["model"]]
                caption_meta.append(meta)

    if unknown:
        raise ValueError(
            "Unknown sourceId(s): "
            + ", ".join(unknown[:8])
            + ". Call list_sources and use exact ids."
        )
    if not pairs:
        raise ValueError(
            "No valid captions in videos[]. Each entry needs sourceId + captions[]"
        )

    caps_text = ws.unique_captions_from_pairs(pairs)
    from .. import chat_store

    pairs = chat_store.enrich_render_pairs_from_catalog(uid, pairs)
    saved = _save_plan(
        uid,
        ctx,
        {
            "renderPairs": pairs,
            "captions": caps_text,
            "captionMeta": caption_meta,
            "mode": "custom",
        },
        new_plan=bool(args.get("newPlan")),
        title=str(args.get("title") or "").strip(),
    )
    show_ui = bool(args.get("showUi"))
    return {
        "ok": True,
        "mode": "custom",
        "pairs": pairs,
        "videos": groups_out,
        "count": len(pairs),
        "videoCount": len(groups_out),
        "captionCount": len(seen_caps),
        "origin": origin,
        "model": model,
        "planId": saved.get("id") or "",
        "planTitle": saved.get("title") or "",
        "newPlan": bool(args.get("newPlan")),
        "openPreviewPopup": show_ui,
    }


def apply_carousel_plan(
    uid: str,
    ctx: ToolContext,
    args: dict | None = None,
) -> dict[str, Any]:
    """Save a photo-carousel plan (slides + one description caption each)."""
    from .. import images as image_lib
    from ..config import MAX_CAROUSEL_SLIDES
    from ..ingest import IngestError

    args = args or {}
    origin = str(args.get("origin") or "chat").strip() or "chat"
    model = str(args.get("model") or "").strip()
    rows: list[dict[str, Any]] = []
    explicit = args.get("carousels")
    if isinstance(explicit, list) and explicit:
        for raw in explicit:
            if not isinstance(raw, dict):
                continue
            cap = str(raw.get("caption") or "").strip()
            slides_in = [str(s).strip() for s in (raw.get("slides") or []) if str(s).strip()]
            if not cap or not slides_in:
                continue
            if len(slides_in) > MAX_CAROUSEL_SLIDES:
                slides_in = slides_in[:MAX_CAROUSEL_SLIDES]
            resolved: list[str] = []
            for token in slides_in:
                img = image_lib.resolve_slide(uid, token)
                resolved.append(str(img.get("id") or token))
            cid = str(raw.get("carouselId") or "").strip() or uuid.uuid4().hex[:10]
            row: dict[str, Any] = {
                "kind": "carousel",
                "carouselId": cid,
                "sourceId": cid,
                "slides": resolved,
                "caption": cap,
                "sourceName": f"{len(resolved)}-slide carousel",
                "origin": origin,
            }
            if model:
                row["model"] = model
            rows.append(row)
    else:
        count = int(args.get("count") or 0)
        slide_count = int(args.get("slideCount") or 0)
        image_ids = [str(x).strip() for x in (args.get("imageIds") or []) if str(x).strip()]
        captions = [str(c).strip() for c in (args.get("captions") or []) if str(c).strip()]
        if not count or not slide_count or not image_ids:
            raise ValueError(
                "Provide carousels[] or count + slideCount + imageIds + captions"
            )
        if len(captions) < count:
            raise ValueError(f"Need {count} captions for {count} carousels")
        try:
            groups = image_lib.chunk_slides(image_ids, slide_count, count)
        except IngestError as exc:
            raise ValueError(str(exc)) from exc
        resolved_groups: list[list[str]] = []
        for group in groups:
            ids: list[str] = []
            for token in group:
                img = image_lib.resolve_slide(uid, token)
                ids.append(str(img.get("id") or token))
            resolved_groups.append(ids)
        for i, slides in enumerate(resolved_groups):
            cid = uuid.uuid4().hex[:10]
            row = {
                "kind": "carousel",
                "carouselId": cid,
                "sourceId": cid,
                "slides": slides,
                "caption": captions[i],
                "sourceName": f"{len(slides)}-slide carousel",
                "origin": origin,
            }
            if model:
                row["model"] = model
            rows.append(row)
    if not rows:
        raise ValueError("No valid carousel rows")

    from .. import catalog
    from .. import chat_store

    for row in rows:
        catalog.record_carousel(
            uid,
            carousel_id=row["carouselId"],
            slides=row["slides"],
            caption=row["caption"],
            chat_thread_id=ctx.thread_id or "",
            created_via="chat",
        )
        row["outputRel"] = f"carousels/{row['carouselId']}"

    pairs = chat_store.enrich_render_pairs_from_catalog(uid, rows)
    caps_text = ws.unique_captions_from_pairs(pairs)
    saved = _save_plan(
        uid,
        ctx,
        {
            "renderPairs": pairs,
            "captions": caps_text,
            "mode": "custom",
        },
        new_plan=bool(args.get("newPlan")),
        title=str(args.get("title") or "").strip(),
    )
    show_ui = bool(args.get("showUi"))
    return {
        "ok": True,
        "kind": "carousel",
        "mode": "custom",
        "pairs": pairs,
        "count": len(pairs),
        "slideCount": len(pairs[0]["slides"]) if pairs else 0,
        "origin": origin,
        "planId": saved.get("id") or "",
        "planTitle": saved.get("title") or "",
        "newPlan": bool(args.get("newPlan")),
        "openPreviewPopup": show_ui,
    }


def build_render_plan(
    uid: str,
    ctx: ToolContext,
    args: dict | None = None,
) -> dict[str, Any]:
    """Build the video×caption plan shown in the chat Preview spreadsheet."""
    args = args or {}
    w = _load_plan(uid, ctx)
    srcs = ctx.list_sources(uid)
    by_id = {s["id"]: s for s in srcs}
    meta_by_cap = _caption_meta_index(w)

    def _enrich(raw_pairs: list[dict], mode: str) -> list[dict]:
        out = []
        for i, p in enumerate(raw_pairs):
            if not isinstance(p, dict):
                continue
            cap = str(p.get("caption") or "").strip()
            slides = [str(s).strip() for s in (p.get("slides") or []) if str(s).strip()]
            if p.get("kind") == "carousel" or slides:
                if not cap or not slides:
                    continue
                cid = str(p.get("carouselId") or p.get("sourceId") or "").strip()
                row = dict(p)
                row.update(
                    {
                        "kind": "carousel",
                        "carouselId": cid,
                        "sourceId": cid,
                        "slides": slides[:5],
                        "caption": cap,
                        "sourceName": p.get("sourceName")
                        or f"{len(slides)}-slide carousel",
                        "captionIndex": i,
                    }
                )
                out.append(_pair_origin_fields(row, mode=mode, meta_by_cap=meta_by_cap))
                continue
            sid = str(p.get("sourceId") or "").strip()
            if not sid or not cap or sid not in by_id:
                continue
            src = by_id[sid]
            base = {
                "sourceId": sid,
                "sourceName": src.get("name"),
                "caption": cap,
                "captionIndex": i,
            }
            for key in ("model", "origin", "sources", "pinUrl"):
                if p.get(key):
                    base[key] = p.get(key)
            out.append(_pair_origin_fields(base, mode=mode, meta_by_cap=meta_by_cap))
        return out

    explicit = args.get("pairs")
    stored = w.get("renderPairs") or []
    saved: dict[str, Any] = w if isinstance(w, dict) else {}
    new_plan = bool(args.get("newPlan"))
    title = str(args.get("title") or "").strip()
    if explicit:
        pairs = _enrich(explicit, "custom")
        if not pairs:
            raise ValueError(
                "No valid pairs — check sourceIds against list_sources "
                "(or slides[] for photo carousels)"
            )
        caps_text = ws.unique_captions_from_pairs(pairs)
        saved = _save_plan(
            uid,
            ctx,
            {
                "renderPairs": pairs,
                "captions": caps_text or w.get("captions") or "",
            },
            new_plan=new_plan,
            title=title,
        )
        mode = "custom"
    elif stored:
        pairs = _enrich(stored, "custom")
        mode = "custom"
    elif ctx.thread_id:
        pairs = []
        mode = "custom"
    else:
        mode = args.get("mode") or w.get("mode") or "matrix"
        if mode == "custom":
            mode = "matrix"
        captions = ws.caption_list(w)
        pairs = []
        if mode == "pair":
            n = min(len(srcs), len(captions))
            for i in range(n):
                pairs.append(
                    _pair_origin_fields(
                        {
                            "sourceId": srcs[i]["id"],
                            "sourceName": srcs[i].get("name"),
                            "caption": captions[i],
                            "captionIndex": i,
                        },
                        mode=mode,
                        meta_by_cap=meta_by_cap,
                    )
                )
        else:
            for s in srcs:
                for ci, c in enumerate(captions):
                    pairs.append(
                        _pair_origin_fields(
                            {
                                "sourceId": s["id"],
                                "sourceName": s.get("name"),
                                "caption": c,
                                "captionIndex": ci,
                            },
                            mode=mode,
                            meta_by_cap=meta_by_cap,
                        )
                    )
    video_ids = {p["sourceId"] for p in pairs}
    caption_texts = {p["caption"] for p in pairs}
    show_ui = bool(args.get("showUi"))
    if pairs and ctx.thread_id and not explicit:
        caps_text = ws.unique_captions_from_pairs(pairs)
        saved = _save_plan(
            uid,
            ctx,
            {
                "renderPairs": pairs,
                "captions": caps_text or w.get("captions") or "",
                "mode": mode,
                "captionMeta": w.get("captionMeta") or [],
            },
        )
    return {
        "ok": True,
        "mode": mode,
        "pairs": pairs,
        # Agent automation must not open UI unless showUi was requested.
        "openPreviewPopup": show_ui,
        "count": len(pairs),
        "videoCount": len(video_ids),
        "captionCount": len(caption_texts),
        "planId": saved.get("id") or "",
        "planTitle": saved.get("title") or "",
        "newPlan": bool(explicit) and new_plan,
    }


def execute(uid: str, name: str, args: dict | None, ctx: ToolContext) -> dict[str, Any]:
    args = args or {}
    try:
        return _execute(uid, name, args, ctx)
    except (HooksError, IngestError, PinterestError, EncodeError, ValueError, KeyError, FacebookError) as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        _log.exception("Tool %s failed", name)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def _execute(uid: str, name: str, args: dict, ctx: ToolContext) -> dict[str, Any]:
    if name == "list_sources":
        srcs = ctx.list_sources(uid)
        slim = [
            {
                "id": s["id"],
                "name": s.get("name"),
                "duration": s.get("duration"),
                "width": s.get("width"),
                "height": s.get("height"),
                "endCaption": s.get("endCaption"),
                "hasCrop": bool(s.get("crop") and s["crop"].get("enabled")),
                "trim": s.get("trim"),
                "url": s.get("url"),
                "platform": s.get("platform"),
            }
            for s in srcs
        ]
        return {"ok": True, "sources": slim}

    if name == "list_images":
        from .. import images as image_lib

        items = [image_lib.slim_for_agent(i) for i in image_lib.list_images(uid)]
        attached: list[dict] = []
        if ctx.thread_id:
            try:
                from .. import chat_store

                thread = chat_store.get_thread(uid, ctx.thread_id)
                for msg in reversed(thread.get("messages") or []):
                    if msg.get("role") != "user":
                        continue
                    for att in msg.get("attachments") or []:
                        if not isinstance(att, dict):
                            continue
                        if (att.get("kind") or "") != "image":
                            continue
                        lid = str(att.get("libraryId") or "").strip()
                        iid = str(att.get("imageId") or "").strip()
                        token = lid or iid
                        if not token:
                            continue
                        try:
                            img = image_lib.resolve_slide(uid, token)
                            attached.append(image_lib.slim_for_agent(img))
                        except Exception:
                            attached.append(
                                {
                                    "id": token,
                                    "name": att.get("name") or token,
                                    "origin": "chat",
                                }
                            )
                    break
            except Exception:
                pass
        return {
            "ok": True,
            "images": items,
            "attachedInOrder": attached,
            "count": len(items),
        }

    if name == "get_workspace":
        plan = _load_plan(uid, ctx)
        source = "thread" if ctx.thread_id else "create"
        return {"ok": True, "workspace": plan, "source": source}

    if name == "set_captions":
        w = _save_plan(
            uid,
            ctx,
            {
                "captions": str(args.get("captions") or ""),
                "renderPairs": [],
                "captionMeta": [],
            },
        )
        caps = ws.caption_list(w)
        return {"ok": True, "captionCount": len(caps)}

    if name == "add_captions":
        raw = args.get("captions")
        if isinstance(raw, list):
            block = "\n".join(str(x) for x in raw if str(x).strip())
        else:
            block = str(raw or "")
        cur = _load_plan(uid, ctx)
        before = {c.lower() for c in ws.caption_list(cur)}
        existing = (cur.get("captions") or "").rstrip()
        merged = (existing + "\n" + block).strip() if existing else block.strip()
        w = _save_plan(uid, ctx, {"captions": merged})
        return {"ok": True, "captionCount": len(ws.caption_list(w))}

    if name == "set_render_plan":
        return apply_render_plan(uid, ctx, args)

    if name == "set_carousel_plan":
        return apply_carousel_plan(uid, ctx, args)

    if name == "ask_models":
        return _ask_models(uid, ctx, args)

    if name == "list_plans":
        from .. import plans as plan_store

        workspace = plan_store.list_plans(uid)
        chat_listed: dict[str, Any] = {"plans": [], "activePlanId": ""}
        if ctx.thread_id:
            from .. import chat_store

            chat_listed = chat_store.list_thread_plans(uid, ctx.thread_id)
        ws_plans = list(workspace.get("plans") or [])
        ws_ids = {str(p.get("id") or "") for p in ws_plans}
        extra = []
        for row in chat_listed.get("plans") or []:
            rid = str(row.get("id") or "")
            if rid and rid not in ws_ids:
                extra.append({**row, "source": "chat"})
        return {
            "ok": True,
            "plans": ws_plans + extra,
            "activePlanId": workspace.get("activePlanId")
            or chat_listed.get("activePlanId")
            or "",
            "count": len(ws_plans) + len(extra),
        }

    if name == "list_offers":
        return {"ok": True, "offers": hooks.list_offers(uid)}

    if name == "get_offer":
        offer = hooks.get_offer(uid, args["offerId"])
        try:
            hooks.pin_active_offer(uid, args["offerId"])
        except HooksError:
            pass
        return {"ok": True, "offer": offer}

    if name == "list_offer_captions":
        limit = args.get("limit")
        bank = hooks.list_caption_bank(
            uid,
            args["offerId"],
            limit=int(limit) if limit else hooks.CAPTION_BANK_CAP,
        )
        return {
            "ok": True,
            "offerId": args["offerId"],
            "pastCaptions": bank,
            "count": len(bank),
        }

    if name == "set_active_offer":
        offer = hooks.set_active_offer(uid, args["offerId"])
        return {"ok": True, "offer": offer, "offerId": args["offerId"]}

    if name == "create_offer":
        offer = hooks.create_offer(
            uid, args["name"], url=args.get("url"), notes=args.get("notes")
        )
        try:
            hooks.pin_active_offer(uid, offer["id"])
        except HooksError:
            pass
        return {"ok": True, "offer": offer}

    if name == "add_winner":
        return {
            "ok": True,
            "winner": hooks.add_winner(uid, args["offerId"], args["text"]),
        }

    if name == "add_like":
        return {"ok": True, "like": hooks.add_like(uid, args["offerId"], args["text"])}

    if name == "generate_hooks":
        result = hooks.generate_hooks(
            uid,
            args["offerId"],
            args.get("count") or 5,
            args.get("mode") or "new_angles",
            args.get("extraInstructions"),
            thread_id=ctx.thread_id or "",
            random_words=bool(args["randomWords"])
            if "randomWords" in args
            else False,
        )
        return {"ok": True, **result}

    if name == "set_style":
        w = ws.save_workspace(uid, {"style": args.get("style") or {}})
        return {"ok": True, "style": w.get("style")}

    if name == "set_end_caption":
        payload = {
            "enabled": bool(args.get("enabled", True)),
            "text": (args.get("text") or "").strip(),
            "durationSeconds": float(args.get("durationSeconds") or 3),
            "xOffset": float(args.get("xOffset") or 0),
            "yOffset": float(args.get("yOffset") or 0),
        }
        if args.get("applyToAll"):
            ws.save_workspace(uid, {"endCaptionTemplate": payload})
            srcs = ctx.list_sources(uid)
            updated = []
            for s in srcs:
                updated.append(ctx.set_end_caption(uid, s["id"], payload))
            return {"ok": True, "applied": len(updated), "template": payload}
        sid = args.get("sourceId")
        if not sid:
            raise ValueError("sourceId or applyToAll required")
        src = ctx.set_end_caption(uid, sid, payload)
        return {"ok": True, "source": src}

    if name == "set_batch_options":
        patch = {k: args[k] for k in ("mode", "nameTemplate", "skipExisting", "dedupe") if k in args}
        if patch.get("mode") in ("matrix", "pair"):
            patch["renderPairs"] = []
        if ctx.thread_id:
            plan_patch = {k: patch[k] for k in ("mode", "renderPairs") if k in patch}
            if plan_patch:
                _save_plan(uid, ctx, plan_patch)
            w = ws.load_workspace(uid)
            ws_patch = {k: patch[k] for k in ("nameTemplate", "skipExisting", "dedupe") if k in patch}
            if ws_patch:
                w = ws.save_workspace(uid, ws_patch)
        else:
            w = ws.save_workspace(uid, patch)
        return {"ok": True, "workspace": {k: w[k] for k in ("mode", "nameTemplate", "skipExisting", "dedupe")}}

    if name == "list_outputs":
        return {"ok": True, **ctx.list_outputs(uid, int(args.get("limit") or 100))}

    if name == "list_tiktok_accounts":
        accounts = tiktok_accounts.list_accounts(uid)
        slim = [
            {
                "id": a.get("id"),
                "name": a.get("name"),
                "enabled": a.get("enabled"),
                "hasCookies": bool(a.get("hasCookies")),
            }
            for a in accounts
        ]
        settings = tiktok_accounts.load_settings(uid)
        return {"ok": True, "accounts": slim, "settings": settings}

    if name == "get_batch_status":
        batch = jobs.get_batch(args["batchId"], uid)
        if not batch:
            raise KeyError("Batch not found")
        return {"ok": True, **jobs.batch_state(batch)}

    if name == "get_upload_status":
        job = tiktok_upload.get_job(args["uploadId"], uid)
        if not job:
            raise KeyError("Upload not found")
        return {"ok": True, **tiktok_upload.job_state(job)}

    if name == "create_custom_sound":
        from .. import studio_sound

        account_id = str(args.get("accountId") or args.get("account_id") or "").strip()
        if not account_id:
            raise ValueError("accountId required")
        return studio_sound.start_custom_sound(
            uid,
            account_id=account_id,
            video_path=str(args.get("videoPath") or args.get("video_path") or ""),
            audio_path=str(args.get("audioPath") or args.get("audio_path") or ""),
            image_path=str(args.get("imagePath") or args.get("image_path") or ""),
            source_id=str(args.get("sourceId") or args.get("source_id") or ""),
            caption=str(args.get("caption") or ""),
        )

    if name == "get_custom_sound":
        from .. import studio_sound

        return studio_sound.get_custom_sound(
            uid,
            job_id=str(args.get("jobId") or args.get("uploadId") or ""),
            account_id=str(args.get("accountId") or args.get("account_id") or ""),
            query=str(args.get("query") or ""),
        )

    if name == "preview_plan":
        return build_render_plan(uid, ctx, args)

    if name == "search_pinterest":
        media = str(args.get("mediaType") or "video").strip().lower()
        if media not in ("video", "image", "any"):
            media = "video"
        result = search_pins(
            str(args.get("query") or ""),
            media=media,
            limit=int(args.get("limit") or 12),
            cursor=(args.get("cursor") or None),
            exclude_urls=list(args.get("excludeUrls") or []),
        )
        pins = normalize_pins(result.get("pins") or [])
        pages = int(result.get("pages") or 0)
        ctx.last_pinterest_pins = list(pins)
        if ctx.thread_id and pins:
            try:
                from .. import chat_store

                chat_store.merge_pinterest_bank(uid, ctx.thread_id, pins)
            except Exception:
                _log.debug("Pinterest bank merge failed", exc_info=True)
        try:
            from .. import usage as usage_mod

            usage_mod.record_pinterest(
                uid,
                thread_id=getattr(ctx, "thread_id", "") or "",
                credits=pages,
            )
        except Exception:
            _log.debug("Pinterest usage record failed", exc_info=True)
        return {
            "ok": True,
            "media": result.get("media") or media,
            "query": result.get("query"),
            "pins": pins,
            "cursor": result.get("cursor"),
            "count": len(pins),
            "_visionUrls": [p.get("thumbnail") for p in pins if p.get("thumbnail")],
        }

    if name == "pinterest_set":
        action = str(args.get("action") or "show").strip().lower()
        if action not in ("show", "download"):
            raise ValueError("pinterest_set action must be show or download")
        mode = str(args.get("mode") or "replace").strip().lower()
        if mode not in ("replace", "merge"):
            mode = "replace"
        pins = normalize_pins(args.get("pins") or [])
        query = str(args.get("query") or "").strip()
        if pins:
            ctx.last_pinterest_pins = list(pins)
        if ctx.thread_id:
            try:
                from .. import chat_store

                if pins:
                    chat_store.merge_pinterest_bank(uid, ctx.thread_id, pins)
                if action == "show":
                    chat_store.set_pinterest_shown(
                        uid, ctx.thread_id, pins, mode=mode
                    )
            except Exception:
                _log.debug("pinterest_set bank update failed", exc_info=True)
        out = {
            "ok": True,
            "action": action,
            "mode": mode,
            "query": query,
            "pins": pins,
            "count": len(pins),
        }
        if action == "show" and pins:
            out["_visionUrls"] = [
                p.get("thumbnail") for p in pins if p.get("thumbnail")
            ]
        return out

    if name == "inspect_sources":
        ids = list(args.get("sourceIds") or [])[:6]
        srcs = {s["id"]: s for s in ctx.list_sources(uid)}
        previews = user_previews_dir(uid)
        frames = []
        vision_paths: list[str] = []
        for sid in ids:
            src = srcs.get(sid)
            if not src or not src.get("path"):
                continue
            dur = float(src.get("duration") or 0) or 1.0
            stamps = [0.5]
            if dur > 3:
                stamps.append(round(dur * 0.4, 2))
            video = Path(src["path"])
            if not video.is_file():
                try:
                    rel = storage.rel_of(uid, video)
                    restored = storage.ensure_local(uid, rel)
                    if restored and restored.is_file():
                        video = restored
                except Exception:
                    pass
            if not video.is_file():
                continue
            for at in stamps:
                outp = previews / f"frame_{src.get('hash') or sid}_t{at:.2f}.jpg"
                try:
                    if not outp.is_file():
                        extract_frame(video, outp, at_seconds=at)
                    frames.append(
                        {
                            "sourceId": sid,
                            "sourceName": src.get("name"),
                            "at": at,
                        }
                    )
                    vision_paths.append(str(outp))
                except EncodeError:
                    continue
        return {"ok": True, "frames": frames, "_visionPaths": vision_paths}

    if name == "analyze_example_ad":
        from .. import tiktok_analyze as ta
        from .. import usage as usage_mod

        url = str(args.get("url") or "").strip()
        if not url:
            raise ValueError("url is required")
        try:
            brief = ta.analyze_example_ad(
                url, get_transcript=bool(args.get("getTranscript"))
            )
        except ta.TikTokAnalyzeError as exc:
            raise ValueError(str(exc)) from exc
        try:
            charged = int(brief.get("creditsCharged") or 1)
            usage_mod.record_pinterest(
                uid,
                thread_id=getattr(ctx, "thread_id", "") or "",
                credits=max(1, charged),
            )
        except Exception:
            _log.debug("example-ad usage record failed", exc_info=True)
        return brief

    if name == "find_carousel_formats":
        from ..carousel_formats import CarouselFormatError, find_carousel_formats
        from ..config import user_previews_dir

        profile = str(args.get("profileUrl") or args.get("profile_url") or "").strip()
        raw_urls = args.get("urls") or []
        if isinstance(raw_urls, str):
            urls = [raw_urls]
        elif isinstance(raw_urls, list):
            urls = [str(u) for u in raw_urls if str(u).strip()]
        else:
            urls = []
        try:
            limit = int(args.get("limit") or 8)
        except (TypeError, ValueError):
            limit = 8
        work = user_previews_dir(uid) / "carousel_formats"
        try:
            return find_carousel_formats(
                profile_url=profile, urls=urls, limit=limit, work_dir=work
            )
        except CarouselFormatError as exc:
            raise ValueError(str(exc)) from exc

    if name == "propose_ideas":
        # Loop intercepts this tool before execute; keep a no-op for MCP.
        ideas = args.get("ideas") or []
        return {
            "ok": True,
            "targetCount": int(args.get("targetCount") or len(ideas) or 0),
            "count": len(ideas),
            "pausedForReview": True,
        }

    if name == "add_source_url":
        url = args["url"]
        path = download_url(uid, url)
        src = ctx.register_downloaded(uid, path, "url", url)
        return {"ok": True, "source": {"id": src["id"], "name": src.get("name")}}

    if name == "add_source_urls":
        urls = [str(u).strip() for u in (args.get("urls") or []) if str(u).strip()]
        if not urls:
            raise ValueError("No URLs to download")
        sources = []
        errors = []
        for url in urls:
            try:
                path = download_url(uid, url)
                src = ctx.register_downloaded(uid, path, "url", url)
                sources.append({"id": src["id"], "name": src.get("name"), "url": url})
            except IngestError as exc:
                errors.append({"url": url, "error": str(exc)})
        if not sources:
            raise IngestError(errors[0]["error"] if errors else "Downloads failed")
        return {"ok": True, "sources": sources, "errors": errors}

    if name == "start_render":
        result = ctx.start_batch_from_pairs(uid, args)
        return {"ok": True, **result}

    if name == "start_tiktok_upload":
        settings = tiktok_accounts.load_settings(uid)
        template = tiktok_upload.resolve_caption_template(
            args.get("captionTemplate"), settings
        )
        cont = bool(settings.get("continueOnError", True))
        batch_id = args.get("batchId")
        if not batch_id:
            raise ValueError("batchId required")
        batch = jobs.get_batch(batch_id, uid)
        videos = tiktok_upload.collect_videos_for_batch(uid, batch_id)
        if not videos:
            raise ValueError("No finished videos in this batch")
        account_id = str(args.get("accountId") or "").strip() or None
        if not account_id:
            thread_id = str(getattr(batch, "chat_thread_id", "") or "").strip() if batch else ""
            if thread_id:
                try:
                    from .. import chat_store

                    plan = chat_store.get_thread_plan(uid, thread_id)
                    account_id = (
                        str((plan or {}).get("tiktokAccountId") or "").strip() or None
                    )
                except Exception:
                    account_id = None
        job = tiktok_upload.start_upload(
            uid,
            videos,
            caption_template=template,
            continue_on_error=cont,
            source_batch_id=batch_id,
            account_id=account_id,
        )
        return {"ok": True, "uploadId": job.id, **tiktok_upload.job_state(job)}

    if name == "start_carousel_upload":
        from .. import chat_store

        if not ctx.thread_id:
            raise ValueError("Carousel upload needs an active chat thread")
        plan = chat_store.get_thread_plan(uid, ctx.thread_id)
        pairs = [p for p in (plan.get("renderPairs") or []) if isinstance(p, dict)]
        carousels = [
            p
            for p in pairs
            if (p.get("kind") == "carousel" or p.get("slides"))
            and str(p.get("caption") or "").strip()
        ]
        if not carousels:
            raise ValueError("No carousel rows in the active plan — call set_carousel_plan first")
        settings = tiktok_accounts.load_settings(uid)
        template = tiktok_upload.resolve_caption_template(
            args.get("captionTemplate"), settings
        )
        account_id = str(args.get("accountId") or plan.get("tiktokAccountId") or "").strip() or None
        sound_query = str(args.get("soundQuery") or args.get("sound_query") or "").strip()
        if args.get("useOriginalSound") and not sound_query:
            sound_query = "original sound"
        job = tiktok_upload.start_carousel_upload(
            uid,
            carousels,
            caption_template=template,
            continue_on_error=bool(settings.get("continueOnError", True)),
            account_id=account_id,
            thread_id=ctx.thread_id,
            sound_query=sound_query,
            sound_path=str(args.get("soundPath") or args.get("sound_path") or "").strip(),
        )
        # Blocking wait removed for Worker/MCP (start + poll get_upload_status).
        # In-app chat may still pass wait; honor only when CAPTION_ALLOW_TOOL_WAIT=1.
        if bool(args.get("wait")) and os.environ.get("CAPTION_ALLOW_TOOL_WAIT") == "1":
            timeout = float(os.environ.get("CAROUSEL_UPLOAD_WAIT_TIMEOUT") or 2400)
            deadline = time.time() + max(30.0, timeout)
            while time.time() < deadline:
                job = tiktok_upload.get_job(job.id, uid) or job
                if job.status in ("done", "cancelled"):
                    break
                time.sleep(4)
            job = tiktok_upload.get_job(job.id, uid) or job
        return {
            "ok": True,
            "uploadId": job.id,
            "waited": False,
            "hint": "Poll get_upload_status with uploadId until done",
            **tiktok_upload.job_state(job),
        }

    if name == "fetch_spark":
        from .. import chat_store
        from .. import spark_jobs
        from ..duoplus import DuoPlusError, format_duoplus_user_error, looks_like_spark_code

        if not ctx.thread_id:
            raise ValueError("Spark fetch needs an active chat thread")
        plan = chat_store.get_thread_plan(uid, ctx.thread_id)
        wanted = args.get("pairs")
        if not isinstance(wanted, list) or not wanted:
            wanted = [
                {"sourceId": p.get("sourceId"), "caption": p.get("caption")}
                for p in (plan.get("renderPairs") or [])
                if isinstance(p, dict)
                and p.get("sourceId")
                and str(p.get("caption") or "").strip()
            ]
        try:
            job = spark_jobs.start_plan_spark(
                uid,
                ctx.thread_id,
                plan_id=str(plan.get("id") or "") or None,
                pairs=wanted,
            )
        except DuoPlusError as exc:
            raise ValueError(format_duoplus_user_error(exc)) from exc
        wait = bool(args.get("wait")) and os.environ.get("CAPTION_ALLOW_TOOL_WAIT") == "1"
        if wait:
            timeout = float(os.environ.get("SPARK_WAIT_TIMEOUT") or 1800)
            deadline = time.time() + max(30.0, timeout)
            while time.time() < deadline:
                if job.status in ("done", "cancelled"):
                    break
                time.sleep(3)
        state = spark_jobs.job_state(job)
        codes = [
            {
                "carouselId": it.get("sourceId"),
                "sourceId": it.get("sourceId"),
                "caption": it.get("caption"),
                "sparkCode": it.get("sparkCode") or "",
                "postUrl": it.get("postUrl") or "",
                "status": it.get("status"),
            }
            for it in (state.get("items") or [])
            if looks_like_spark_code(str(it.get("sparkCode") or ""))
        ]
        return {
            "ok": True,
            "waited": bool(wait),
            "jobId": job.id,
            "hint": "Poll get_spark_job with jobId until status is done",
            "job": state,
            "codes": codes,
            "sparkCodes": [c["sparkCode"] for c in codes],
        }

    if name == "list_facebook_accounts":
        st = facebook_ads.public_status(uid)
        accounts = []
        if st.get("connected"):
            try:
                accounts = facebook_ads.list_ad_accounts(uid)
            except FacebookError as exc:
                return {"ok": False, "error": str(exc), **st}
        return {"ok": True, **st, "adAccounts": accounts}

    if name == "list_facebook_campaigns":
        items = facebook_ads.list_campaigns(uid, args.get("adAccountId"))
        return {"ok": True, "campaigns": items}

    if name == "list_facebook_adsets":
        items = facebook_ads.list_adsets(
            uid,
            campaign_id=args.get("campaignId"),
            ad_account_id=args.get("adAccountId"),
        )
        return {"ok": True, "adsets": items}

    if name == "list_facebook_pixels":
        items = facebook_ads.list_pixels(uid, args.get("adAccountId"))
        return {"ok": True, "pixels": items}

    if name == "start_facebook_ads":
        landing = (args.get("landingUrl") or "").strip()
        if not landing:
            raise ValueError("landingUrl is required")
        mode = args.get("mode") or "new_campaign"
        videos: list[dict] = []
        batch_id = args.get("batchId")
        if batch_id:
            collected = tiktok_upload.collect_videos_for_batch(uid, batch_id)
            paths = []
            for v in collected:
                rel = str(v.get("relPath") or "")
                if rel:
                    paths.append(rel)
                    continue
                p = Path(v.get("videoPath") or "")
                try:
                    rel = storage.rel_of(uid, p)
                    if rel.startswith("output/"):
                        rel = rel[len("output/") :]
                    paths.append(rel)
                except Exception:
                    continue
            if not paths:
                raise ValueError("No finished videos in this batch")
            videos = facebook_jobs.videos_from_paths(uid, paths)
        elif args.get("paths"):
            videos = facebook_jobs.videos_from_paths(uid, args["paths"])
        elif args.get("useLatestBatch"):
            outs = ctx.list_outputs(uid, 50)
            paths = [i["path"] for i in (outs.get("items") or [])[:20]]
            if not paths:
                raise ValueError("No rendered outputs to upload")
            videos = facebook_jobs.videos_from_paths(uid, paths)
        else:
            raise ValueError("paths, batchId, or useLatestBatch required")
        job = facebook_jobs.start_job(
            uid,
            videos,
            mode=mode,
            landing_url=landing,
            campaign_id=args.get("campaignId"),
            adset_id=args.get("adsetId"),
            campaign_name=args.get("campaignName"),
            adset_name=args.get("adsetName"),
            daily_budget=args.get("dailyBudget"),
            bid_strategy=args.get("bidStrategy"),
            bid_amount=args.get("bidAmount"),
            pixel_id=args.get("pixelId"),
            custom_event_type=args.get("customEventType"),
            page_id=args.get("pageId"),
        )
        return {"ok": True, "jobId": job.id, **facebook_jobs.job_state(job)}

    if name == "delete_sources":
        ids = args.get("sourceIds") or []
        for sid in ids:
            ctx.delete_source(uid, sid)
        return {"ok": True, "deleted": len(ids)}

    if name == "delete_outputs":
        return {"ok": True, **ctx.delete_outputs(uid, args.get("paths") or [])}

    if name == "get_bc_settings":
        return {"ok": True, **_bc_settings_public(uid)}

    if name == "set_bc_settings":
        from ..provision.finance import card as card_mod

        amount = args.get("adAccountBudgetUsd")
        if amount is None:
            amount = args.get("amountUsd")
        account_name = " ".join(str(args.get("accountName") or "").split())
        patch: dict[str, Any] = {}
        touched = False
        if amount is not None:
            touched = True
            if account_name:
                card_mod.set_ad_account_budget(
                    amount_usd=float(amount),
                    account_name=account_name,
                    uid=uid,
                )
            else:
                patch["adAccountBudgetUsd"] = max(0.0, float(amount))
        if args.get("extraAdAccounts") is not None:
            touched = True
            patch["extraAdAccounts"] = max(0, min(20, int(args["extraAdAccounts"])))
        if args.get("addBalanceAmount") is not None:
            touched = True
            patch["addBalanceAmount"] = max(10.0, float(args["addBalanceAmount"]))
        if args.get("mercuryDailyLimit") is not None:
            touched = True
            patch["mercuryDailyLimit"] = max(0.0, float(args["mercuryDailyLimit"]))
        spark = args.get("sparkCode")
        if spark is None:
            spark = args.get("sparkCodeDefault")
        if spark is not None and str(spark).strip():
            touched = True
            card_mod.add_spark_code(str(spark), uid=uid, select=True)
        vat_id = str(args.get("vatId") or "").strip()
        if vat_id:
            touched = True
            patch["vatId"] = vat_id
        card_id = str(args.get("cardId") or "").strip()
        if card_id:
            touched = True
            card_mod.select_card(card_id, uid=uid)
        host_id = str(args.get("activeHostId") or "").strip()
        if host_id:
            from ..provision import hosts as hosts_mod
            from ..provision.hosts import HostConfigError

            try:
                hosts_mod.set_active_host(host_id, uid=uid)
            except HostConfigError as exc:
                raise ValueError(str(exc)) from exc
            touched = True
            try:
                from .. import provision_embed

                provision_embed.restart_embedded_worker()
            except Exception:
                pass
        sales_map = {
            "salesConversionBidUsd": lambda v: max(0.0, float(v)),
            "salesDailyBudgetUsd": lambda v: max(0.0, float(v)),
            "salesLandingUrl": lambda v: str(v or "").strip(),
            "salesInstantPageCta": lambda v: str(v or "").strip(),
            "salesCreativeCta": lambda v: str(v or "").strip(),
            "salesDisplayCard": lambda v: (
                v if isinstance(v, bool) else str(v).strip().lower() not in {"0", "false", "no", "off", ""}
            ),
            "salesAdNameTemplate": lambda v: str(v or "").strip(),
            "salesCreativeText": lambda v: str(v or "").strip(),
        }
        for key, coerce in sales_map.items():
            if args.get(key) is None:
                continue
            touched = True
            patch[key] = coerce(args[key])
        if patch:
            card_mod.save_finance_settings(patch, uid=uid)
        elif not touched:
            raise ValueError(
                "Pass adAccountBudgetUsd (day-one one-time budget), sparkCode, "
                "salesConversionBidUsd, cardId, activeHostId, or another BC field"
            )
        return {"ok": True, **_bc_settings_public(uid)}

    if name == "list_bcs":
        from ..provision import bc_inventory as bc_inv

        inv_data = bc_inv.build_inventory(uid)
        rows = list(inv_data.get("profiles") or [])
        view = str(args.get("view") or "").strip().lower()
        if view:
            rows = [r for r in rows if str(r.get("view") or "") == view]
        q = str(args.get("query") or "").strip().lower()
        if q:
            filtered = []
            for r in rows:
                blob = " ".join(
                    [
                        str(r.get("email") or ""),
                        str(r.get("bcName") or ""),
                        str(r.get("notes") or ""),
                        str(r.get("id") or ""),
                        " ".join(
                            str(a.get("accountId") or "")
                            for a in (r.get("accounts") or [])
                            if isinstance(a, dict)
                        ),
                    ]
                ).lower()
                if q in blob:
                    filtered.append(r)
            rows = filtered
        slim = []
        for r in rows[:100]:
            slim.append(
                {
                    "id": r.get("id"),
                    "email": r.get("email"),
                    "bcName": r.get("bcName"),
                    "status": r.get("status"),
                    "view": r.get("view"),
                    "adsReady": r.get("adsReady"),
                    "paymentRequired": r.get("paymentRequired"),
                    "suspensionStatus": r.get("suspensionStatus"),
                    "notes": r.get("notes"),
                    "accounts": [
                        {
                            "accountId": a.get("accountId"),
                            "accountName": a.get("accountName"),
                            "whiteHatScheduled": bool(a.get("whiteHatScheduled")),
                            "advertiserAvailable": (
                                a.get("advertiserAvailable")
                                if isinstance(a.get("advertiserAvailable"), bool)
                                else None
                            ),
                            "suspensionStatus": a.get("suspensionStatus") or None,
                            "tiktokAdvertiserStatus": a.get("tiktokAdvertiserStatus") or "",
                        }
                        for a in (r.get("accounts") or [])
                        if isinstance(a, dict)
                    ],
                    "accountCount": len(r.get("accounts") or []),
                    "sparkPackCount": len(r.get("sparkPack") or []),
                    "jobId": r.get("jobId"),
                }
            )
        return {
            "ok": True,
            "count": len(slim),
            "profiles": slim,
            "worker": inv_data.get("worker"),
            "runPreview": inv_data.get("runPreview"),
        }

    if name == "get_bc":
        from ..provision import bc_inventory as bc_inv
        from ..provision import inventory as inv

        pid = str(args.get("profileId") or "").strip()
        row = inv.get_profile(uid, pid)
        if not row:
            raise ValueError(f"Profile not found: {pid}")
        return {"ok": True, "profile": bc_inv.enrich_profile(uid, row)}

    if name == "preview_spark_bc_launch":
        from ..provision.spark_launch import SparkLaunchError, preview_spark_launch

        codes = list(args.get("sparkCodes") or [])
        text = str(args.get("sparkText") or "").strip()
        try:
            return preview_spark_launch(
                uid,
                intended_offer_ids=list(args.get("intendedOfferIds") or []),
                spark_codes=codes or text or None,
                accounts_per_bc=args.get("accountsPerBc"),
                extra_ad_accounts=args.get("extraAdAccounts"),
                bc_count=args.get("bcCount"),
                geo=str(args.get("geo") or "SE"),
            )
        except SparkLaunchError as exc:
            raise ValueError(str(exc)) from exc

    if name == "start_bc_warmup":
        from .. import provision_jobs
        from ..provision.inventory import InventoryError

        bc_count = args.get("bcCount")
        if bc_count is None:
            bc_count = args.get("count")
        try:
            job = provision_jobs.start_provision(
                uid,
                count=int(bc_count) if bc_count is not None else 1,
                geo=str(args.get("geo") or "SE"),
                login_mode="auto",
                reason=str(args.get("reason") or ""),
                intended_offer_ids=list(args.get("intendedOfferIds") or []),
                extra_ad_accounts=args.get("extraAdAccounts"),
                accounts_per_bc=args.get("accountsPerBc"),
                launch_sales=False,
            )
        except InventoryError as exc:
            raise ValueError(str(exc)) from exc
        state = provision_jobs.job_state(job)
        return {
            "ok": True,
            "job": state,
            "bcCount": len(state.get("items") or []) or 1,
            "adAccounts": int(state.get("accountsPerBc") or 1),
        }

    if name == "start_spark_bc_launch":
        from .. import provision_jobs
        from ..provision.inventory import InventoryError

        codes = list(args.get("sparkCodes") or [])
        text = str(args.get("sparkText") or "").strip()
        try:
            job = provision_jobs.start_provision(
                uid,
                count=args.get("bcCount"),
                geo=str(args.get("geo") or "SE"),
                login_mode="auto",
                reason=str(args.get("reason") or ""),
                intended_offer_ids=list(args.get("intendedOfferIds") or []),
                extra_ad_accounts=args.get("extraAdAccounts"),
                accounts_per_bc=args.get("accountsPerBc"),
                spark_codes=codes or text or None,
                launch_sales=True,
            )
        except InventoryError as exc:
            raise ValueError(str(exc)) from exc
        state = provision_jobs.job_state(job)
        return {
            "ok": True,
            "job": state,
            "bcCount": len(state.get("items") or []),
            "sparkCount": state.get("sparkCount"),
            "hint": "Poll get_bc_job until status is done; Sales enqueue after white-hat Scheduled",
        }

    if name == "get_bc_job":
        from .. import provision_jobs

        jid = str(args.get("jobId") or "").strip()
        job = provision_jobs.get_job(uid, jid)
        if not job:
            raise ValueError(f"Job not found: {jid}")
        return {"ok": True, "job": provision_jobs.job_state(job)}

    if name == "cancel_bc_job":
        from .. import provision_jobs

        jid = str(args.get("jobId") or "").strip()
        job = provision_jobs.cancel_job(uid, jid)
        if not job:
            raise ValueError(f"Job not found: {jid}")
        return {"ok": True, "job": provision_jobs.job_state(job)}

    if name == "mark_bc_suspended":
        from ..provision import bc_inventory as bc_inv
        from ..provision.inventory import InventoryError

        pid = str(args.get("profileId") or "").strip()
        scope = str(args.get("scope") or "profile").strip().lower()
        try:
            if scope == "account":
                account = bc_inv.mark_account_suspended(
                    uid,
                    profile_id=pid,
                    account_id=str(args.get("accountId") or ""),
                    account_name=str(args.get("accountName") or ""),
                    reason_code=str(args.get("reasonCode") or "unknown"),
                    reason=str(args.get("reason") or ""),
                )
                return {"ok": True, "account": account}
            profile = bc_inv.mark_profile_suspended(
                uid,
                pid,
                reason_code=str(args.get("reasonCode") or "unknown"),
                reason=str(args.get("reason") or ""),
            )
            return {"ok": True, "profile": profile}
        except InventoryError as exc:
            raise ValueError(str(exc)) from exc

    if name == "mark_bc_disapproved":
        from ..provision import bc_inventory as bc_inv
        from ..provision.inventory import InventoryError

        pid = str(args.get("profileId") or "").strip()
        process_refund = args.get("processRefund")
        if process_refund is None:
            process_refund = True
        try:
            profile = bc_inv.mark_profile_disapproved(
                uid,
                pid,
                reason=str(args.get("reason") or ""),
                how="manual",
                process_refund=bool(process_refund),
            )
            return {"ok": True, "profile": profile, "refund": profile.get("refund")}
        except InventoryError as exc:
            raise ValueError(str(exc)) from exc

    if name == "mark_white_hat_approved":
        from ..provision import bc_inventory as bc_inv
        from ..provision.inventory import InventoryError

        try:
            account = bc_inv.mark_white_hat_approved(
                uid,
                profile_id=str(args.get("profileId") or ""),
                account_id=str(args.get("accountId") or ""),
                account_name=str(args.get("accountName") or ""),
            )
        except InventoryError as exc:
            raise ValueError(str(exc)) from exc
        return {"ok": True, "account": account}

    if name == "continue_bc":
        from .. import provision_jobs
        from ..provision.inventory import InventoryError

        pid = str(args.get("profileId") or "").strip()
        if not pid:
            raise ValueError("profileId required")
        try:
            job = provision_jobs.continue_profile(uid, pid)
        except InventoryError as exc:
            raise ValueError(str(exc)) from exc
        return {"ok": True, "profileId": pid, "job": provision_jobs.job_state(job)}

    if name == "mark_bc_payment":
        from fastapi import HTTPException

        from ..routers.provision import MarkPaymentAddedBody, provision_mark_payment_added

        pid = str(args.get("profileId") or "").strip()
        if not pid:
            raise ValueError("profileId required")
        body_kwargs: dict[str, Any] = {}
        if args.get("amountUsd") not in (None, ""):
            body_kwargs["amountUsd"] = float(args.get("amountUsd") or 0)
        try:
            return provision_mark_payment_added(
                pid, MarkPaymentAddedBody(**body_kwargs), uid=uid
            )
        except HTTPException as exc:
            detail = exc.detail
            raise ValueError(detail if isinstance(detail, str) else str(detail)) from exc

    if name == "set_bc_notes":
        from ..provision import bc_inventory as bc_inv
        from ..provision import inventory as inv
        from ..provision.inventory import InventoryError

        pid = str(args.get("profileId") or "").strip()
        try:
            inv.update_profile(uid, pid, notes=str(args.get("notes") or ""))
            inv.append_event(uid, pid, "notes_updated")
        except InventoryError as exc:
            raise ValueError(str(exc)) from exc
        row = inv.get_profile(uid, pid)
        if not row:
            raise ValueError(f"Profile not found: {pid}")
        return {"ok": True, "profile": bc_inv.enrich_profile(uid, row)}

    if name == "open_bc_ads_manager":
        from ..provision import inventory as inv
        from ..provision.inventory import InventoryError

        pid = str(args.get("profileId") or "").strip()
        row = inv.get_profile(uid, pid)
        if not row:
            raise ValueError(f"Profile not found: {pid}")
        ap = str(row.get("adspowerProfileId") or "").strip()
        if not ap:
            raise ValueError("Profile has no AdsPower id yet")
        worker = inv.worker_status(uid)
        if not worker.get("online"):
            raise ValueError("Provision worker is offline")
        try:
            step = inv.enqueue_step(
                uid,
                kind="open_ads_manager",
                profile_id=pid,
                payload={
                    "adspowerProfileId": ap,
                    "accountRef": str(args.get("accountRef") or "1").strip() or "1",
                },
            )
            inv.append_event(
                uid,
                pid,
                "open_ads_enqueued",
                stepId=step.get("id"),
                accountRef=args.get("accountRef"),
            )
        except InventoryError as exc:
            raise ValueError(str(exc)) from exc
        return {"ok": True, "step": step, "worker": worker}

    if name == "list_sparks":
        from ..affiliate.service import list_spark_rows

        page = list_spark_rows(uid)
        return {"ok": True, "sparks": page.get("sparks") or [], "offers": page.get("offers") or []}

    if name == "get_tiktok_connection":
        from ..tiktok_marketing.store import public_connection, get_connection

        return {"ok": True, "connection": public_connection(get_connection(uid, str(args.get("profileId") or "")))}

    if name == "sync_tiktok_insights":
        from ..tiktok_marketing.reports import sync_all

        return {"ok": True, **sync_all(uid)}

    if name == "list_topups":
        from ..tiktok_marketing.store import list_topups

        return {"ok": True, "topups": list_topups(uid)}

    if name in ("update_campaign_bid", "update_campaign_budget", "set_campaign_status"):
        from ..tiktok_marketing.launch import set_operation_status, update_bid, update_budget
        from ..tiktok_marketing.store import connection_token

        token = connection_token(uid, str(args.get("profileId") or ""))
        if not token:
            raise ValueError("This profile is not connected to the Marketing API")
        advertiser_id = str(args.get("advertiserId") or "")
        if name == "update_campaign_bid":
            data = update_bid(token, advertiser_id, str(args.get("adgroupId") or ""), float(args.get("bid") or 0), lead=bool(args.get("lead")))
        elif name == "update_campaign_budget":
            data = update_budget(token, advertiser_id, str(args.get("adgroupId") or ""), float(args.get("budget") or 0))
        else:
            data = set_operation_status(token, advertiser_id, str(args.get("campaignId") or ""), str(args.get("status") or ""))
        return {"ok": True, "result": data}

    if name == "approve_topup":
        from ..tiktok_marketing.store import save_topup

        return {"ok": True, "topup": save_topup(uid, str(args.get("topupId") or ""), {"status": "approved"})}

    if name == "ensure_dummy_pixel":
        from ..provision import inventory as inv
        from ..provision.finance import accounts as acct_mod
        from ..tiktok_marketing.client import TikTokApiError
        from ..tiktok_marketing.launch import token_for_advertiser
        from ..tiktok_marketing.pixel import ensure_website_pixel

        pid = str(args.get("profileId") or "").strip()
        if not pid:
            raise ValueError("profileId required")
        row = inv.get_profile(uid, pid)
        if not row:
            raise ValueError(f"Profile not found: {pid}")
        advertiser_id = str(args.get("advertiserId") or "").strip()
        account = str(args.get("account") or "").strip()
        rows = acct_mod.find_accounts(uid=uid, profile_id=pid)
        if not advertiser_id:
            if account.isdigit():
                index = int(account) - 1
                if 0 <= index < len(rows):
                    advertiser_id = str(rows[index].get("accountId") or "")
            if not advertiser_id and account:
                for item in rows:
                    if str(item.get("accountId") or "") == account or str(item.get("accountName") or "") == account:
                        advertiser_id = str(item.get("accountId") or "")
                        break
            if not advertiser_id and rows:
                advertiser_id = str(rows[0].get("accountId") or "")
        if not advertiser_id:
            raise ValueError("Ad account has no advertiser id")
        token = token_for_advertiser(uid, pid, advertiser_id)
        if not token:
            raise ValueError("This Business Center is not connected to the Marketing API")
        try:
            pixel = ensure_website_pixel(
                token,
                advertiser_id,
                bc_id=str(row.get("hootOrgId") or ""),
            )
        except TikTokApiError as exc:
            raise ValueError(str(exc)) from exc
        return {"ok": True, "profileId": pid, "account": account or "1", **pixel}

    if name == "start_sales_campaign":
        from ..provision import bc_inventory as bc_inv
        from ..provision import inventory as inv
        from ..provision.inventory import InventoryError

        pid = str(args.get("profileId") or "").strip()
        auto_pick = bool(args.get("autoPick"))
        picked: dict | None = None
        needs_pick = (not pid and auto_pick) or (not pid and bool(args.get("sparkCodes")))
        if needs_pick:
            picked = bc_inv.pick_available_load_target(uid)
            if not picked:
                raise ValueError(
                    "No available ads-ready ad account (white-hat Scheduled, sales not done)"
                )
            pid = str(picked.get("profileId") or "").strip()
            if not str(args.get("account") or "").strip():
                args = dict(args)
                args["account"] = str(picked.get("account") or "1")
        if not pid:
            raise ValueError("profileId required (or set autoPick=true)")
        row = inv.get_profile(uid, pid)
        if not row:
            raise ValueError(f"Profile not found: {pid}")
        ap = str(row.get("adspowerProfileId") or "").strip()
        if not ap:
            raise ValueError("Profile has no AdsPower id yet")
        worker = inv.worker_status(uid)
        if not worker.get("online"):
            raise ValueError("Provision worker is offline")
        offer_ids = [
            str(x).strip() for x in (args.get("offerIds") or []) if str(x).strip()
        ]
        if offer_ids:
            try:
                row = inv.update_profile(uid, pid, intendedOfferIds=offer_ids)
            except Exception:
                row = inv.get_profile(uid, pid) or row
        codes = [str(c).strip() for c in (args.get("sparkCodes") or []) if str(c).strip()]
        account = str(args.get("account") or "").strip() or "1"
        from ..provision.spark_launch import sales_payload_for_profile

        payload = sales_payload_for_profile(
            row,
            adspower_profile_id=ap,
            spark_codes=codes or None,
            account=account,
        )
        if offer_ids:
            payload["offerIds"] = offer_ids
        landing = str(args.get("landingUrl") or "").strip()
        if landing:
            payload["landingUrl"] = landing
        for key in (
            "objective",
            "conversionBidUsd",
            "dailyBudgetUsd",
            "bidScope",
            "creatorAccountId",
            "itemIds",
            "postUrl",
            "postUrls",
            "posts",
            "topupAmountUsd",
            "topupThresholdUsd",
            "pageId",
            "advertiserId",
        ):
            if args.get(key) not in (None, ""):
                payload[key] = args.get(key)
        payload["backend"] = "api"
        if args.get("force"):
            payload["force"] = True
        try:
            step = inv.ensure_sales_campaign(
                uid,
                pid,
                payload=payload,
                force=bool(args.get("force")),
            )
        except InventoryError as exc:
            raise ValueError(str(exc)) from exc
        if not step:
            raise ValueError(
                "Sales campaign not queued — white-hat must be Scheduled and Sales not already done"
            )
        try:
            inv.update_profile(uid, pid, status="sales_campaign", lastError=None)
        except Exception:
            pass
        out: dict = {"ok": True, "step": step, "worker": worker, "profileId": pid}
        if picked:
            out["picked"] = picked
        out["account"] = account
        if offer_ids:
            out["offerIds"] = offer_ids
        if landing:
            out["landingUrl"] = landing
        if codes:
            out["sparkCount"] = len(codes)
        return out

    raise ValueError(f"Unknown tool: {name}")


def _bc_settings_public(uid: str) -> dict[str, Any]:
    from ..provision import hosts as hosts_mod
    from ..provision.advertisers.create import extra_ad_account_count, one_ad_account_per_bc
    from ..provision.finance import accounts as acct_mod
    from ..provision.finance import card as card_mod
    from ..provision.finance import catalog
    from ..provision.session import all_tabs

    settings = card_mod.finance_settings(uid=uid)
    vat = catalog.selected_vat(uid=uid)
    per = settings.get("adAccountBudgets")
    hosts = hosts_mod.ensure_hosts(uid=uid)
    return {
        "vatId": str(settings.get("vatId") or vat.get("id") or ""),
        "vats": catalog.list_vats(geo="SE", uid=uid),
        "addBalanceAmount": card_mod.add_balance_amount(uid=uid),
        "mercuryDailyLimit": card_mod.daily_spend_limit(uid=uid),
        "extraAdAccounts": extra_ad_account_count(uid=uid),
        "oneAdAccountPerBc": one_ad_account_per_bc(),
        "adAccountBudgetUsd": card_mod.ad_account_budget_usd(uid=uid),
        "adAccountBudgetMode": card_mod.ad_account_budget_mode(uid=uid),
        "adAccountBudgets": dict(per) if isinstance(per, dict) else {},
        "sparkCodeDefault": card_mod.white_hat_spark_code(uid=uid),
        "sparkCodeId": card_mod.selected_spark_id(uid=uid),
        "sparkCodes": card_mod.list_spark_codes(uid=uid),
        "cardId": card_mod.selected_card_id(uid=uid),
        "cards": card_mod.public_cards(uid=uid),
        "card": card_mod.public_card(uid=uid),
        **card_mod.sales_settings(uid=uid),
        "activeHostId": str(hosts.get("activeHostId") or ""),
        "hosts": hosts_mod.list_hosts(uid=uid),
        "activeHost": hosts_mod.active_host_public(uid=uid),
        "budgets": acct_mod.summary(uid=uid),
        "browserTabs": all_tabs(uid=uid),
    }


def confirm_summary(name: str, args: dict | None) -> str:
    args = args or {}
    if name == "add_source_url":
        return f"Download video from URL:\n{args.get('url')}"
    if name == "add_source_urls":
        urls = args.get("urls") or []
        return f"Download {len(urls)} video URL(s):\n" + "\n".join(str(u) for u in urls[:12])
    if name == "start_render":
        pairs = args.get("pairs")
        if pairs:
            return f"Render {len(pairs)} selected video×caption pair(s)."
        if args.get("mode") == "custom":
            return "Render the saved custom video×caption plan."
        return "Render all videos × captions from the workspace (matrix/pair/custom plan)."
    if name == "start_tiktok_upload":
        if args.get("batchId"):
            return f"Upload batch {args.get('batchId')} to TikTok."
        return "Upload rendered videos to TikTok."
    if name == "find_carousel_formats":
        target = args.get("profileUrl") or args.get("profile_url") or ""
        n = len(args.get("urls") or [])
        if target:
            return f"Scan {target} for photo-carousel formats."
        if n:
            return f"Scan {n} post URL(s) for photo-carousel formats."
        return "Scan posts for photo-carousel formats."
    if name == "start_carousel_upload":
        return "Upload photo carousels from the active plan to TikTok (Studio, native photos)."
    if name == "create_custom_sound":
        return "Post a seed video to create a TikTok original sound, then wait for Studio review."
    if name == "fetch_spark":
        wait = " and wait for codes" if args.get("wait") else ""
        n = len(args.get("pairs") or [])
        extra = f" for {n} row(s)" if n else " for posted plan rows"
        return f"Fetch Spark codes{extra}{wait}."
    if name == "start_facebook_ads":
        n = len(args.get("paths") or [])
        mode = args.get("mode") or "new_campaign"
        dest = {
            "new_campaign": "a new campaign",
            "new_adset": "a new ad set",
            "existing_adset": "an existing ad set",
        }.get(mode, mode)
        count = f"{n} rendered video(s)" if n else "rendered videos"
        return f"Create {count} as paused Facebook ads in {dest}."
    if name == "delete_sources":
        return f"Delete {len(args.get('sourceIds') or [])} source video(s)."
    if name == "delete_outputs":
        return f"Permanently delete {len(args.get('paths') or [])} rendered file(s)."
    if name == "start_bc_warmup":
        n = int(args.get("bcCount") or args.get("count") or 1)
        geo = args.get("geo") or "SE"
        if args.get("accountsPerBc") not in (None, ""):
            accts = f"{int(args.get('accountsPerBc') or 1)} ad account(s) each"
        elif args.get("extraAdAccounts") not in (None, ""):
            accts = f"{1 + int(args.get('extraAdAccounts') or 0)} ad account(s) each"
        else:
            accts = "the ad account count in Settings"
        return (
            f"Warm up {n} Business Center(s) with {accts} "
            f"(geo {geo}, auto login + white-hat)."
        )
    if name == "start_spark_bc_launch":
        codes = args.get("sparkCodes") or []
        n_codes = len(codes) if isinstance(codes, list) else 0
        text = str(args.get("sparkText") or "")
        if not n_codes and text:
            n_codes = len([p for p in text.split() if p.strip()])
        offers = args.get("intendedOfferIds") or []
        accts = int(args.get("accountsPerBc") or 0) or (
            1 + int(args.get("extraAdAccounts") or 0)
        ) or 1
        src = f"{n_codes} pasted Spark(s)" if n_codes else f"{len(offers)} offer(s)"
        return (
            f"Launch BCs + Sales from {src}, {accts} ad account(s) per BC "
            f"(geo {args.get('geo') or 'SE'})."
        )
    if name == "cancel_bc_job":
        return f"Stop BC warm-up job {args.get('jobId')} (resume later from BC)."
    if name == "mark_bc_suspended":
        scope = args.get("scope") or "profile"
        code = args.get("reasonCode") or "unknown"
        return f"Mark {scope} suspended ({code}) on profile {args.get('profileId')}."
    if name == "mark_bc_disapproved":
        return (
            f"Mark profile {args.get('profileId')} disapproved "
            f"(proxy unusable + Hoot banned for refund)."
        )
    if name == "mark_white_hat_approved":
        return (
            f"Manually approve white-hat as Scheduled for profile "
            f"{args.get('profileId')} account {args.get('accountName') or args.get('accountId') or '1'}."
        )
    if name == "continue_bc":
        return f"Continue Business Center profile {args.get('profileId')} from the current step."
    if name == "mark_bc_payment":
        amount = args.get("amountUsd")
        shown = f"${amount}" if amount not in (None, "") else "the default amount"
        return f"Mark payment added ({shown}) on profile {args.get('profileId')}."
    if name == "open_bc_ads_manager":
        return (
            f"Open Ads Manager for profile {args.get('profileId')} "
            f"account {args.get('accountRef') or '1'}."
        )
    if name == "start_sales_campaign":
        bits = []
        if args.get("autoPick") and not args.get("profileId"):
            bits.append("auto-pick available ads-ready account")
        else:
            bits.append(f"profile {args.get('profileId')}")
        if args.get("account"):
            bits.append(f"account {args.get('account')}")
        codes = args.get("sparkCodes") or []
        if codes:
            bits.append(f"{len(codes)} spark(s)")
        offers = args.get("offerIds") or []
        if offers:
            bits.append(f"offer(s) {', '.join(str(x) for x in offers[:3])}")
        if args.get("landingUrl"):
            bits.append(f"url {args.get('landingUrl')}")
        if args.get("conversionBidUsd") not in (None, ""):
            bits.append(f"bid ${args.get('conversionBidUsd')}")
        if args.get("dailyBudgetUsd") not in (None, ""):
            bits.append(f"budget ${args.get('dailyBudgetUsd')}")
        if args.get("force"):
            bits.append("force")
        return "Load sparks → Sales Instant Page (" + "; ".join(bits) + ")."
    return f"Run tool `{name}` with args:\n{json.dumps(args, indent=2)[:800]}"


def idea_deny_key(pin_url: str, caption: str) -> str:
    return f"{(pin_url or '').strip()}||{(caption or '').strip()}"


def normalize_ideas(raw: list | None) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        pin_url = str(item.get("pinUrl") or item.get("url") or "").strip()
        caption = str(item.get("caption") or "").strip()
        title = str(item.get("title") or "").strip()
        if not pin_url:
            continue
        if not caption:
            caption = title or "Video pick"
        key = idea_deny_key(pin_url, caption)
        if key in seen:
            continue
        seen.add(key)
        iid = str(item.get("id") or "").strip() or uuid.uuid4().hex[:10]
        out.append(
            {
                "id": iid,
                "pinUrl": pin_url,
                "caption": caption,
                "thumbnail": str(item.get("thumbnail") or "").strip(),
                "title": title,
                "previewUrl": str(item.get("previewUrl") or "").strip(),
                "previewKind": str(item.get("previewKind") or "").strip(),
                "kind": str(item.get("kind") or "").strip(),
                "imageUrl": str(item.get("imageUrl") or "").strip(),
                "duration": item.get("duration") or "",
                "width": item.get("width") or "",
                "height": item.get("height") or "",
            }
        )
    return out
