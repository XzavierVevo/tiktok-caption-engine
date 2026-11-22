"""Marketing API payload and dealer tests. No network."""

from __future__ import annotations

import unittest
from unittest import mock

from app.tiktok_marketing.balance import effective_topup_mode, resolved_amount, resolved_threshold
from app.tiktok_marketing.client import TikTokApiError
from app.tiktok_marketing.deal import plan_video_campaigns
from app.tiktok_marketing.creative import CARD_PATH, _is_learn_more, _write_display_card
from app.tiktok_marketing.payloads import (
    APPLE_DEVICE_MODEL_IDS,
    ad_body,
    lead_adgroup_body,
    sales_adgroup_body,
    sales_campaign_body,
    website_adgroup_body,
)
from app.tiktok_marketing.health import is_suspended_status, sync_account_suspensions
from app.tiktok_marketing.pixel import pick_dummy_pixel
from app.tiktok_marketing.reports import rollup_spark, sync_all
from app.tiktok_marketing.store import consume_oauth_state, merge_connections, public_connection, save_oauth_state


class PayloadTests(unittest.TestCase):
    def test_pick_dummy_pixel_prefers_website_dummy(self) -> None:
        chosen = pick_dummy_pixel([
            {"asset_id": "1", "asset_name": "other"},
            {"asset_id": "2", "asset_name": "website-dummy", "pixel_code": "ABC"},
        ])
        self.assertEqual(chosen["asset_id"], "2")
        self.assertEqual(pick_dummy_pixel([]) , None)
    def test_learn_more_label(self) -> None:
        self.assertTrue(_is_learn_more("Learn more"))
        self.assertTrue(_is_learn_more("LEARN_MORE"))
        self.assertFalse(_is_learn_more("Shop now"))
        self.assertFalse(_is_learn_more("Sign up"))

    def test_display_card_is_tap_here(self) -> None:
        self.assertEqual(CARD_PATH.name, "tap_here_750x421.jpg")
        from PIL import Image

        image = Image.open(_write_display_card())
        self.assertEqual(image.size, (750, 421))

    def test_sales_destination_is_instant_page_only(self) -> None:
        body = sales_adgroup_body(
            advertiser_id="1",
            campaign_id="2",
            name="offer-1-ac1",
            budget=20,
            bid=0.4,
        )
        self.assertEqual(body["placements"], ["PLACEMENT_TIKTOK"])
        self.assertEqual(body["promotion_website_type"], "TIKTOK_NATIVE_PAGE")
        self.assertEqual(body["optimization_goal"], "CONVERT")
        self.assertEqual(body["optimization_event"], "BUTTON")
        self.assertEqual(body["billing_event"], "OCPM")
        self.assertEqual(body["conversion_bid_price"], 0.4)
        self.assertNotIn("bid_price", body)
        self.assertNotIn("pixel_id", body)
        self.assertTrue(body["video_download_disabled"])
        self.assertTrue(body["share_disabled"])
        self.assertEqual(body["targeting_spec"]["spc_audience_age"], "OVER_EIGHTEEN")
        self.assertNotIn("landing_page_url", body)
        self.assertNotIn("budget_optimize_on", body)
        self.assertEqual(body["budget_mode"], "BUDGET_MODE_DYNAMIC_DAILY_BUDGET")
        campaign = sales_campaign_body(advertiser_id="1", name="offer-1-ac1", publish=False)
        self.assertEqual(campaign["operation_status"], "DISABLE")
        self.assertEqual(campaign["sales_destination"], "WEBSITE")
        self.assertFalse(campaign["budget_optimize_on"])
        self.assertNotIn("budget", campaign)

    def test_sales_adgroup_can_target_apple_only(self) -> None:
        body = sales_adgroup_body(
            advertiser_id="1",
            campaign_id="2",
            name="payload",
            budget=20,
            bid=0.4,
            device_models=APPLE_DEVICE_MODEL_IDS,
        )
        self.assertEqual(body["targeting_spec"]["device_model_ids"], ["202"])

    def test_lead_adgroup_has_request_id_and_targeting_spec(self) -> None:
        body = lead_adgroup_body(
            advertiser_id="1",
            campaign_id="2",
            name="lead",
            budget=20,
            bid=0.4,
            device_models=APPLE_DEVICE_MODEL_IDS,
        )
        self.assertTrue(body["request_id"])
        self.assertEqual(body["targeting_spec"]["location_ids"], ["6252001"])
        self.assertEqual(body["targeting_spec"]["device_model_ids"], ["202"])
        self.assertNotIn("location_ids", body)
        self.assertNotIn("age_groups", body)

    def test_website_conversion_uses_dummy_pixel(self) -> None:
        group = website_adgroup_body(
            advertiser_id="1",
            campaign_id="2",
            name="offer-1-web",
            budget=20,
            bid=0.1,
            pixel_id="px1",
            ios=True,
        )
        self.assertEqual(group["optimization_goal"], "CLICK")
        self.assertEqual(group["billing_event"], "CPC")
        self.assertEqual(group["bid_price"], 0.1)
        self.assertNotIn("conversion_bid_price", group)
        self.assertNotIn("optimization_event", group)
        self.assertEqual(group["promotion_website_type"], "UNSET")
        self.assertEqual(group["pixel_id"], "px1")
        self.assertEqual(group["targeting_optimization_mode"], "MANUAL")
        self.assertEqual(group["targeting_spec"]["device_model_ids"], ["202"])
        self.assertNotIn("operating_systems", group["targeting_spec"])
        body = ad_body(
            advertiser_id="1",
            adgroup_id="2",
            posts=[{"itemId": "99"}],
            identity={"identity_id": "id", "identity_authorized_bc_id": "bc"},
            page_id="",
            name_for=lambda index, _post: f"ad-{index}",
            landing_url="https://rico-lander.YOUR-SUBDOMAIN.workers.dev?s2=264bc1ac1ad1",
        )
        self.assertEqual(
            body["landing_page_url_list"],
            [{"landing_page_url": "https://rico-lander.YOUR-SUBDOMAIN.workers.dev?s2=264bc1ac1ad1"}],
        )
        self.assertNotIn("page_list", body)
        self.assertNotIn("landing_page_url", body)

    def test_ads_use_bc_auth_and_hide_nothing(self) -> None:
        body = ad_body(
            advertiser_id="1",
            adgroup_id="2",
            posts=[{"itemId": "99"}],
            identity={"identity_id": "id", "identity_authorized_bc_id": "bc"},
            page_id="page",
            name_for=lambda index, _post: f"ad-{index}",
        )
        creative = body["creative_list"][0]["creative_info"]
        self.assertEqual(len(body["creative_list"]), 1)
        self.assertEqual(creative["identity_type"], "BC_AUTH_TT")
        self.assertNotIn("dark_post_status", creative)
        self.assertNotIn("call_to_action_list", creative)
        self.assertEqual(body["call_to_action_list"], [{"call_to_action": "LEARN_MORE"}])
        self.assertEqual(body["page_list"], [{"page_id": "page"}])
        self.assertFalse(body["ad_configuration"]["creative_auto_add_toggle"])
        self.assertEqual(body["ad_configuration"]["creative_auto_enhancement_strategy_list"], [])
        self.assertEqual(body["ad_configuration"]["product_info_enabled"], "UNSET")
        self.assertEqual(body["ad_configuration"]["product_info"]["selling_points"], [])
        self.assertEqual(body["ad_configuration"]["promo_info_list"], [])
        self.assertNotIn("landing_page_url", body)
        self.assertNotIn("landing_page_url_list", body)
        self.assertNotIn("playable_list", body)

    def test_display_card_is_the_only_add_on(self) -> None:
        body = ad_body(
            advertiser_id="1",
            adgroup_id="2",
            posts=[{"itemId": "99"}, {"itemId": "100"}],
            identity={"identity_id": "id", "identity_authorized_bc_id": "bc"},
            page_id="page",
            name_for=lambda index, _post: f"ad-{index}",
            call_to_action_id="cta1",
            card_id="card1",
        )
        self.assertEqual(len(body["creative_list"]), 1)
        self.assertEqual(body["creative_list"][0]["creative_info"]["tiktok_item_id"], "99")
        self.assertEqual(body["interactive_add_on_list"], [{"card_id": "card1"}])
        self.assertEqual(body["ad_configuration"]["call_to_action_id"], "cta1")

    def test_display_card_can_be_omitted(self) -> None:
        body = ad_body(
            advertiser_id="1",
            adgroup_id="2",
            posts=[{"itemId": "99"}],
            identity={"identity_id": "id", "identity_authorized_bc_id": "bc"},
            page_id="page",
            name_for=lambda index, _post: f"ad-{index}",
            call_to_action_id="cta1",
        )
        self.assertNotIn("interactive_add_on_list", body)
        self.assertEqual(body["ad_configuration"]["call_to_action_id"], "cta1")

    def test_nonzero_code_is_an_error(self) -> None:
        with self.assertRaises(TikTokApiError):
            raise TikTokApiError("nope", code=40002)

    def test_public_connection_hides_token(self) -> None:
        shown = public_connection({"accessToken": "secret", "oauthStatus": "connected"})
        self.assertNotIn("accessToken", shown)
        self.assertEqual(shown["oauthStatus"], "connected")

    def test_merge_connections_keeps_token_and_unions_advertisers(self) -> None:
        remote = {
            "byId": {
                "p1": {
                    "profileId": "p1",
                    "accessToken": "old-token",
                    "oauthStatus": "connected",
                    "advertiserIds": ["a1"],
                    "updatedAt": "2026-11-14T00:00:00Z",
                },
                "p9": {
                    "profileId": "p9",
                    "accessToken": "remote-only",
                    "advertiserIds": ["a9"],
                    "updatedAt": "2026-11-14T00:00:00Z",
                },
            }
        }
        local = {
            "byId": {
                "p1": {
                    "profileId": "p1",
                    "advertiserIds": ["a2"],
                    "oauthStatus": "pending",
                    "updatedAt": "2026-11-18T00:00:00Z",
                }
            }
        }
        merged = merge_connections(remote, local)
        row = merged["byId"]["p1"]
        self.assertEqual(row["accessToken"], "old-token")
        self.assertEqual(row["oauthStatus"], "connected")
        self.assertEqual(row["advertiserIds"], ["a1", "a2"])
        self.assertIn("p9", merged["byId"])
        self.assertEqual(merged["byId"]["p9"]["accessToken"], "remote-only")

    def test_dealer_groups_one_creator(self) -> None:
        renders = [
            {"tiktok_post_url": f"https://www.tiktok.com/@a/video/{i}", "tiktok_account_id": "a", "offer_id": "o"}
            for i in range(12)
        ]
        slices = plan_video_campaigns(renders, videos_per_campaign=10, offer_or_tag="o")
        self.assertEqual(len(slices), 2)
        self.assertEqual(slices[0]["count"], 10)
        self.assertEqual(slices[1]["count"], 2)

    def test_rollup(self) -> None:
        totals = rollup_spark([
            {"spend": 4, "impressions": 10, "conversions": 2},
            {"spend": 6, "impressions": 5, "conversions": 0},
        ])
        self.assertEqual(totals["spend"], 10)
        self.assertEqual(totals["costPerConversion"], 5)

    def test_topup_amount_override_and_manual_host(self) -> None:
        self.assertEqual(resolved_amount({"topupAmountUsd": 25}, {"topupAmountUsd": 40}), 40)
        self.assertEqual(resolved_amount({"topupAmountUsd": 25}, {}), 25)
        self.assertEqual(resolved_threshold({}, {}), 2)
        self.assertEqual(effective_topup_mode({"topupMode": "automatic"}, {"topupMode": "manual"}), "manual")

    def test_oauth_state_is_single_use(self) -> None:
        with mock.patch("app.tiktok_marketing.store.userstore.patch_kv") as patch_kv:
            saved = {}

            def _patch(_uid, _key, mutator):
                updated = mutator(dict(saved))
                saved.clear()
                saved.update(updated)
                return saved

            patch_kv.side_effect = _patch
            save_oauth_state("dev", "profile-1", "nonce")
            self.assertEqual(saved["byId"]["profile-1"]["oauthState"], "nonce")
            profile_id = consume_oauth_state("dev", "nonce")
            self.assertEqual(profile_id, "profile-1")
            again = consume_oauth_state("dev", "nonce")
            self.assertEqual(again, "")

    def test_marketing_api_sign_in_runs_before_white_hat(self) -> None:
        import inspect

        from app.provision_jobs import _continue_after_hoot
        from app.tiktok_marketing.connect import connect_marketing_api

        src = inspect.getsource(_continue_after_hoot)
        self.assertLess(src.find('kind="tiktok_oauth"'), src.find('kind="white_hat"'))
        from app.tiktok_marketing import connect as connect_mod

        self.assertIn("poll_verification_code", inspect.getsource(connect_mod._hoot_code))
        self.assertIn("Send Code", inspect.getsource(connect_mod._complete_identity_check))
        self.assertIn("solve_same_shape_captcha", inspect.getsource(connect_mod._solve_oauth_captcha))
        self.assertIn("_complete_identity_check", inspect.getsource(connect_mod._run_oauth_page))
        self.assertIn('page.route("**/*oauth/tiktok*"', inspect.getsource(connect_mod._capture_redirect))
        self.assertNotIn('page.route("**/*"', inspect.getsource(connect_mod._capture_redirect))
        self.assertIn("range(1, 4)", inspect.getsource(connect_marketing_api))
        self.assertEqual(
            connect_mod._auth_code_from("YOUR_VALUE?auth_code=abc"),
            "abc",
        )
        class _Page:
            url = "YOUR_VALUE?auth_code=frompage"
            frames = []
        self.assertEqual(
            connect_mod._auth_code_from_page(_Page(), {"url": ""}),
            "frompage",
        )

    def test_reach_white_hat_starts_a_year_out_at_twenty_a_day(self) -> None:
        from datetime import datetime, timezone

        from app.tiktok_marketing.whitehat_api import (
            reach_adgroup_body,
            reach_campaign_body,
            schedule_window,
        )

        start, end = schedule_window()
        start_at = datetime.strptime(start, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        delta = start_at - datetime.now(timezone.utc)
        self.assertGreater(delta.days, 360)
        self.assertLess(delta.days, 370)
        campaign = reach_campaign_body(advertiser_id="1", name="whitehat-264-ac1")
        self.assertEqual(campaign["objective_type"], "REACH")
        self.assertEqual(campaign["operation_status"], "ENABLE")
        group = reach_adgroup_body(
            advertiser_id="1",
            campaign_id="2",
            name="whitehat-264-ac1",
            start=start,
            end=end,
        )
        self.assertEqual(group["budget"], 20.0)
        self.assertEqual(group["bid_type"], "BID_TYPE_NO_BID")
        self.assertNotIn("bid_price", group)
        self.assertEqual(group["schedule_start_time"], start)

    def test_white_hat_not_start_is_scheduled(self) -> None:
        from app.tiktok_marketing.launch import classify_white_hat_secondary, white_hat_from_campaigns

        self.assertEqual(
            classify_white_hat_secondary("CAMPAIGN_STATUS_NOT_START", "ENABLE"),
            "scheduled",
        )
        self.assertEqual(
            classify_white_hat_secondary("CAMPAIGN_STATUS_ADVERTISER_AUDIT", "ENABLE"),
            "pending",
        )
        self.assertEqual(
            classify_white_hat_secondary("CAMPAIGN_STATUS_AUDIT_DENY", "DISABLE"),
            "rejected",
        )
        parsed = white_hat_from_campaigns(
            [{"campaign_name": "whitehat", "secondary_status": "CAMPAIGN_STATUS_NOT_START", "operation_status": "ENABLE"}]
        )
        self.assertEqual(parsed["kind"], "scheduled")
        self.assertTrue(parsed["found"])
        reach = white_hat_from_campaigns([
            {
                "campaign_name": "reco-264-web-ac1",
                "objective_type": "WEB_CONVERSIONS",
                "secondary_status": "ADVERTISER_ACCOUNT_PUNISH",
                "operation_status": "ENABLE",
            },
            {
                "campaign_id": "1881821836096738",
                "campaign_name": "Reach20261114093321",
                "objective_type": "REACH",
                "secondary_status": "ADVERTISER_ACCOUNT_PUNISH",
                "operation_status": "ENABLE",
            },
        ])
        self.assertTrue(reach["found"])
        self.assertEqual(reach["campaignId"], "1881821836096738")
        self.assertEqual(reach["kind"], "pending")
        self.assertEqual(
            classify_white_hat_secondary("CAMPAIGN_STATUS_ENABLE", "ENABLE"),
            "pending",
        )
        self.assertEqual(
            classify_white_hat_secondary("ADGROUP_STATUS_NOT_START", "ENABLE"),
            "scheduled",
        )

    def test_future_adgroup_not_start_marks_scheduled(self) -> None:
        from app.tiktok_marketing.launch import refine_white_hat_status

        campaign = {
            "found": True,
            "kind": "pending",
            "status": "CAMPAIGN_STATUS_ENABLE",
            "secondary": "CAMPAIGN_STATUS_ENABLE",
            "campaignId": "1882157568078945",
        }
        scheduled = refine_white_hat_status(
            campaign,
            [{
                "secondary_status": "ADGROUP_STATUS_NOT_START",
                "operation_status": "ENABLE",
                "schedule_start_time": "2027-11-18 01:29:46",
            }],
        )
        self.assertEqual(scheduled["kind"], "scheduled")
        self.assertEqual(scheduled["scheduleStart"], "2027-11-18 01:29:46")
        in_review = refine_white_hat_status(
            campaign,
            [{
                "secondary_status": "ADGROUP_STATUS_AUDIT",
                "operation_status": "ENABLE",
                "schedule_start_time": "2027-11-18 01:29:46",
            }],
        )
        self.assertEqual(in_review["kind"], "pending")

    def test_api_publish_saves_scheduled_flag(self) -> None:
        from unittest import mock

        from app.tiktok_marketing import whitehat_api as wh

        saved: dict = {}

        def upsert(entry, uid=None):
            del uid
            saved.update(entry)
            return entry

        with mock.patch.object(wh.acct_mod, "upsert_account", side_effect=upsert), mock.patch.object(
            wh.acct_mod,
            "find_accounts",
            return_value=[{"accountId": "111", "whiteHatScheduled": True}],
        ), mock.patch("app.provision.inventory.update_profile") as update_profile:
            wh._remember(
                "uid",
                {"id": "p1", "adspowerProfileId": "ap", "profileNo": "266"},
                "111",
                {
                    "published": True,
                    "kind": "scheduled",
                    "campaignId": "99",
                    "secondary": "ADGROUP_STATUS_NOT_START",
                },
            )
        self.assertTrue(saved["whiteHatScheduled"])
        self.assertEqual(saved["whiteHatStatus"], "scheduled")
        self.assertEqual(saved["whiteHatCampaignId"], "99")
        self.assertEqual(saved["whiteHatError"], "")
        update_profile.assert_called()
        self.assertEqual(update_profile.call_args.kwargs["status"], "ready")

    def test_api_publish_watches_until_the_future_start_is_scheduled(self) -> None:
        from unittest import mock

        from app.tiktok_marketing import whitehat_api as wh

        saved: dict = {}

        def upsert(entry, uid=None):
            del uid
            saved.update(entry)
            return entry

        with mock.patch.object(wh.acct_mod, "upsert_account", side_effect=upsert), mock.patch(
            "app.provision.inventory.ensure_white_hat_monitor"
        ) as monitor, mock.patch("app.provision.inventory.update_profile"):
            wh._remember(
                "uid",
                {"id": "p1", "adspowerProfileId": "ap", "profileNo": "266"},
                "111",
                {
                    "published": True,
                    "kind": "pending",
                    "campaignId": "99",
                    "secondary": "ADGROUP_STATUS_AUDIT",
                },
            )
        self.assertTrue(saved["whiteHatCreated"])
        self.assertNotIn("whiteHatScheduled", saved)
        self.assertEqual(saved["whiteHatStatus"], "submitted")
        monitor.assert_called_once()
        self.assertEqual(monitor.call_args.kwargs["payload"]["accounts"], ["111"])
        self.assertTrue(monitor.call_args.kwargs["payload"]["immediate"])

    def test_tiktok_status_limit_is_suspended(self) -> None:
        self.assertTrue(is_suspended_status("STATUS_LIMIT"))
        self.assertTrue(is_suspended_status("STATUS_DISABLE"))
        self.assertTrue(is_suspended_status("STATUS_ENABLE", secondary="ADVERTISER_ACCOUNT_PUNISH"))
        self.assertFalse(is_suspended_status("STATUS_ENABLE"))
        self.assertFalse(is_suspended_status(""))

    def test_sync_all_reads_advertiser_suspensions(self) -> None:
        with mock.patch(
            "app.tiktok_marketing.health.sync_account_suspensions",
            return_value={"checked": 1, "marked": [{"advertiserId": "1", "status": "STATUS_LIMIT"}]},
        ) as health, mock.patch(
            "app.tiktok_marketing.store.iter_token_rows",
            return_value=[],
        ), mock.patch(
            "app.tiktok_marketing.reports.list_campaigns",
            return_value=[],
        ):
            out = sync_all("dev")
        health.assert_called_once_with("dev")
        self.assertEqual(out["suspensions"]["marked"][0]["status"], "STATUS_LIMIT")

    def test_read_advertisers_fills_missing_id(self) -> None:
        with mock.patch(
            "app.tiktok_marketing.health.tiktok_request",
            return_value={"list": [{"status": "STATUS_LIMIT", "name": "264"}]},
        ):
            from app.tiktok_marketing.health import read_advertisers

            rows = read_advertisers("t", ["7694419107789340673"])
        self.assertEqual(rows[0]["advertiser_id"], "7694419107789340673")
        self.assertEqual(rows[0]["status"], "STATUS_LIMIT")

    def test_sync_account_suspensions_marks_policy(self) -> None:
        with mock.patch(
            "app.tiktok_marketing.health.iter_token_rows",
            return_value=[{"accessToken": "t", "profileId": "p1", "advertiserIds": ["769"]}],
        ), mock.patch(
            "app.tiktok_marketing.health.read_advertisers",
            return_value=[{"advertiser_id": "769", "status": "STATUS_LIMIT", "rejection_reason": ""}],
        ), mock.patch(
            "app.provision.finance.accounts.find_accounts",
            return_value=[{"accountId": "769", "profileId": "p1", "suspensionStatus": ""}],
        ) as find, mock.patch(
            "app.provision.bc_inventory.mark_account_suspended",
            return_value={"accountId": "769"},
        ) as mark:
            out = sync_account_suspensions("dev")
        find.assert_called()
        mark.assert_called_once()
        kwargs = mark.call_args.kwargs
        self.assertEqual(kwargs["how"], "tiktok")
        self.assertEqual(kwargs["tiktok_status"], "STATUS_LIMIT")
        self.assertEqual(out["marked"][0]["advertiserId"], "769")

    def test_sync_account_suspensions_clears_tiktok_mark(self) -> None:
        with mock.patch(
            "app.tiktok_marketing.health.iter_token_rows",
            return_value=[{"accessToken": "t", "profileId": "p1", "advertiserIds": ["769"]}],
        ), mock.patch(
            "app.tiktok_marketing.health.read_advertisers",
            return_value=[{"advertiser_id": "769", "status": "STATUS_ENABLE"}],
        ), mock.patch(
            "app.provision.finance.accounts.find_accounts",
            return_value=[{
                "accountId": "769",
                "profileId": "p1",
                "suspensionStatus": "suspended",
                "suspendedHow": "tiktok",
            }],
        ), mock.patch(
            "app.provision.bc_inventory.clear_account_suspended",
            return_value={"accountId": "769"},
        ) as clear:
            out = sync_account_suspensions("dev")
        clear.assert_called_once()
        self.assertEqual(clear.call_args.kwargs["tiktok_status"], "STATUS_ENABLE")
        self.assertTrue(clear.call_args.kwargs["advertiser_available"])
        self.assertEqual(out["cleared"], ["769"])

    def test_sync_leaves_manual_suspension(self) -> None:
        with mock.patch(
            "app.tiktok_marketing.health.iter_token_rows",
            return_value=[{"accessToken": "t", "profileId": "p1", "advertiserIds": ["769"]}],
        ), mock.patch(
            "app.tiktok_marketing.health.read_advertisers",
            return_value=[{"advertiser_id": "769", "status": "STATUS_ENABLE"}],
        ), mock.patch(
            "app.provision.finance.accounts.find_accounts",
            return_value=[{
                "accountId": "769",
                "profileId": "p1",
                "suspensionStatus": "suspended",
                "suspendedHow": "manual",
                "tiktokAdvertiserStatus": "",
                "advertiserAvailable": False,
            }],
        ), mock.patch(
            "app.provision.finance.accounts.upsert_account",
            return_value={},
        ) as upsert, mock.patch(
            "app.provision.bc_inventory.clear_account_suspended",
        ) as clear:
            sync_account_suspensions("dev")
        clear.assert_not_called()
        upsert.assert_called_once()
        self.assertTrue(upsert.call_args.args[0]["advertiserAvailable"])


class ClairIdentityTests(unittest.TestCase):
    def test_extra_accounts_bind_tt_account_asset(self) -> None:
        from app.tiktok_marketing.identity import assign_tt_account_to_advertisers

        assigned: list[tuple[str, str, str]] = []

        def assign(token, bc, asset, advertiser_id):
            assigned.append((token, bc, str(advertiser_id)))
            return {}

        with mock.patch("app.tiktok_marketing.identity.assign_to_advertiser", side_effect=assign):
            out = assign_tt_account_to_advertisers(
                "acct",
                "acct-bc",
                "tt-1",
                ["7709286955439112210", "7709287597893337140"],
            )
        self.assertEqual(out["7709286955439112210"], "assigned")
        self.assertEqual(out["7709287597893337140"], "assigned")
        self.assertEqual(
            assigned,
            [
                ("acct", "acct-bc", "7709286955439112210"),
                ("acct", "acct-bc", "7709287597893337140"),
            ],
        )

    def test_loading_assigns_clair_to_every_ad_account(self) -> None:
        from app.tiktok_marketing.posted_sales import grant_hub_creator

        assigned: list[str] = []

        def assign(_token, _bc, _asset, advertiser_ids):
            assigned.extend(str(item) for item in advertiser_ids)
            return {str(item): "assigned" for item in advertiser_ids}

        identity = {
            "identity_id": "clair",
            "identity_type": "BC_AUTH_TT",
            "can_pull_video": True,
            "display_name": "clairw34",
        }
        with (
            mock.patch(
                "app.tiktok_marketing.posted_sales.list_tt_assets",
                return_value=[{"asset_id": "hub-tt"}],
            ),
            mock.patch("app.tiktok_marketing.posted_sales.add_partner", return_value={}),
            mock.patch(
                "app.tiktok_marketing.posted_sales.list_advertiser_assets",
                return_value=["acc-1", "acc-2", "acc-3"],
            ),
            mock.patch(
                "app.tiktok_marketing.posted_sales.share_advertisers_to_partner",
                return_value={},
            ) as share_ads,
            mock.patch(
                "app.tiktok_marketing.posted_sales.assign_tt_account_to_advertisers",
                side_effect=assign,
            ) as bind,
            mock.patch(
                "app.tiktok_marketing.posted_sales.list_identities",
                return_value=[identity],
            ),
        ):
            got = grant_hub_creator(
                hub_token="hub",
                account_token="acct",
                hub_bc="hub-bc",
                account_bc="acct-bc",
                advertiser_id="acc-3",
                advertiser_ids=["acc-1", "acc-2", "acc-3"],
            )
        self.assertEqual(got["identity_id"], "clair")
        self.assertEqual(assigned, ["acc-3", "acc-1", "acc-2"])
        self.assertEqual(bind.call_args.args, ("hub", "hub-bc", "hub-tt", ["acc-3", "acc-1", "acc-2"]))
        self.assertEqual(
            share_ads.call_args.args,
            ("acct", "acct-bc", "hub-bc", ["acc-3", "acc-1", "acc-2"]),
        )

    def test_profile_lists_every_advertiser(self) -> None:
        from app.tiktok_marketing.posted_sales import _advertiser_ids_on_profile

        rows = [
            {"accountId": "7694419180409520180"},
            {"accountId": "7709286955439112210"},
            {"accountId": "7709287597893337140"},
        ]
        with mock.patch("app.tiktok_marketing.posted_sales.acct_mod.find_accounts", return_value=rows), mock.patch(
            "app.tiktok_marketing.posted_sales.get_connection",
            return_value={"advertiserIds": ["7709286955439112210", "7709287597893337140"]},
        ):
            ids = _advertiser_ids_on_profile(
                "uid",
                {"id": "1a6639ca567e"},
                {"account": "3"},
                "7709287597893337140",
            )
        self.assertEqual(
            ids,
            [
                "7709287597893337140",
                "7694419180409520180",
                "7709286955439112210",
            ],
        )

    def test_bc_asset_list_adds_accounts_the_profile_row_missed(self) -> None:
        from app.tiktok_marketing.posted_sales import grant_hub_creator

        assigned: list[str] = []

        def assign(_token, _bc, _asset, advertiser_ids):
            assigned.extend(str(item) for item in advertiser_ids)
            return {str(item): "assigned" for item in advertiser_ids}

        identity = {
            "identity_id": "clair",
            "identity_type": "BC_AUTH_TT",
            "can_pull_video": True,
        }
        with (
            mock.patch(
                "app.tiktok_marketing.posted_sales.list_tt_assets",
                return_value=[{"asset_id": "hub-tt"}],
            ),
            mock.patch("app.tiktok_marketing.posted_sales.add_partner", return_value={}),
            mock.patch(
                "app.tiktok_marketing.posted_sales.list_advertiser_assets",
                return_value=["acc-1", "acc-2", "acc-3"],
            ),
            mock.patch("app.tiktok_marketing.posted_sales.share_advertisers_to_partner", return_value={}),
            mock.patch(
                "app.tiktok_marketing.posted_sales.assign_tt_account_to_advertisers",
                side_effect=assign,
            ),
            mock.patch("app.tiktok_marketing.posted_sales.list_identities", return_value=[identity]),
        ):
            grant_hub_creator(
                hub_token="hub",
                account_token="acct",
                hub_bc="hub-bc",
                account_bc="acct-bc",
                advertiser_id="acc-1",
                advertiser_ids=["acc-1"],
            )
        self.assertEqual(assigned, ["acc-1", "acc-2", "acc-3"])


class CarouselAdTests(unittest.TestCase):
    def test_photo_item_type_is_carousel_ads(self) -> None:
        from app.tiktok_marketing.identity import ad_format_for_item_type

        self.assertEqual(ad_format_for_item_type("CAROUSEL"), "CAROUSEL_ADS")
        self.assertEqual(ad_format_for_item_type("VIDEO"), "SINGLE_VIDEO")

    def test_photo_urls_and_lookup_set_format_per_post(self) -> None:
        from app.tiktok_marketing.posted_sales import _posts_for_items

        posts = _posts_for_items(
            {
                "postUrls": [
                    "https://www.tiktok.com/@clairw34/photo/111",
                    "https://www.tiktok.com/@clairw34/video/222",
                ]
            },
            ["111", "222", "333"],
            {"333": "CAROUSEL_ADS"},
        )
        self.assertEqual(posts[0]["adFormat"], "CAROUSEL_ADS")
        self.assertEqual(posts[0]["postUrl"], "https://www.tiktok.com/@clairw34/photo/111")
        self.assertEqual(posts[1]["adFormat"], "SINGLE_VIDEO")
        self.assertEqual(posts[1]["postUrl"], "https://www.tiktok.com/@clairw34/video/222")
        self.assertEqual(posts[2]["adFormat"], "CAROUSEL_ADS")

    def test_item_lookup_reads_carousel_type(self) -> None:
        from app.tiktok_marketing.identity import item_ad_formats

        pages = [
            {
                "video_list": [{"item_id": "1", "item_type": "VIDEO"}],
                "has_more": True,
                "cursor": "next",
            },
            {
                "video_list": [{"item_id": "7709635925034274062", "item_type": "CAROUSEL"}],
                "has_more": False,
                "cursor": "",
            },
        ]

        def request(_method, _path, _token, params=None, **_kwargs):
            self.assertEqual(params.get("count"), 20)
            return pages.pop(0)

        with mock.patch("app.tiktok_marketing.identity.tiktok_request", side_effect=request):
            formats = item_ad_formats(
                "token",
                "adv",
                {"identity_id": "clair", "identity_authorized_bc_id": "bc"},
                ["7709635925034274062", "1"],
            )
        self.assertEqual(formats["7709635925034274062"], "CAROUSEL_ADS")
        self.assertEqual(formats["1"], "SINGLE_VIDEO")

    def test_rejected_video_format_retries_as_carousel(self) -> None:
        from app.tiktok_marketing.client import TikTokApiError
        from app.tiktok_marketing.launch import create_sales_ads

        bodies: list[dict] = []

        def request(_method, _path, _token, json_body=None, **_kwargs):
            bodies.append(json_body)
            fmt = json_body["creative_list"][0]["creative_info"]["ad_format"]
            if fmt == "SINGLE_VIDEO":
                raise TikTokApiError(
                    "Invalid param(s): tiktok_item_id 7709635925034274062's creative type doesn't match ad_format."
                )
            return {"smart_plus_ad_id": "ad-carousel"}

        with (
            mock.patch("app.tiktok_marketing.creative.learn_more_cta_id", return_value="cta"),
            mock.patch("app.tiktok_marketing.creative.display_card_id", return_value=""),
            mock.patch("app.tiktok_marketing.launch.card_mod.sales_settings", return_value={}),
            mock.patch("app.tiktok_marketing.launch.tiktok_request", side_effect=request),
        ):
            ids = create_sales_ads(
                "token",
                advertiser_id="adv",
                adgroup_id="group",
                posts=[{"itemId": "7709635925034274062", "adName": "reco-1"}],
                identity={"identity_id": "clair", "identity_authorized_bc_id": "bc"},
                page_id="page",
                name_for=lambda index, _post: f"ad-{index}",
                display_card=False,
            )
        self.assertEqual(ids, ["ad-carousel"])
        self.assertEqual(bodies[0]["creative_list"][0]["creative_info"]["ad_format"], "SINGLE_VIDEO")
        self.assertEqual(bodies[1]["creative_list"][0]["creative_info"]["ad_format"], "CAROUSEL_ADS")
        self.assertEqual(
            bodies[1]["creative_list"][0]["creative_info"]["tiktok_item_id"],
            "7709635925034274062",
        )

    def test_each_post_keeps_its_format_and_its_own_name(self) -> None:
        from app.tiktok_marketing.payloads import ad_body, sales_ad_name
        from app.tiktok_marketing.posted_sales import _posts_for_items

        posts = _posts_for_items(
            {
                "posts": [
                    {"itemId": "111", "adFormat": "CAROUSEL_ADS"},
                    {"itemId": "222", "adFormat": "SINGLE_VIDEO"},
                ],
                "postUrls": [
                    "https://www.tiktok.com/@clairw34/photo/111",
                    "https://www.tiktok.com/@clairw34/video/222",
                ],
            },
            ["111", "222"],
        )
        names = []
        bodies = []
        for index, post in enumerate(posts, 1):
            name = sales_ad_name(
                "reco-{i}",
                offer="Rico",
                index=index,
                item_id=post["itemId"],
                ad_format=post["adFormat"],
            )
            names.append(name)
            bodies.append(
                ad_body(
                    advertiser_id="adv",
                    adgroup_id="group",
                    posts=[{**post, "adName": name}],
                    identity={"identity_id": "clair", "identity_authorized_bc_id": "bc"},
                    page_id=f"page-{index}",
                    name_for=lambda _i, _post, label=name: label,
                )
            )
        self.assertEqual(names[0], "reco-1-111-carousel")
        self.assertEqual(names[1], "reco-2-222-video")
        self.assertNotEqual(names[0], names[1])
        self.assertEqual(bodies[0]["creative_list"][0]["creative_info"]["ad_format"], "CAROUSEL_ADS")
        self.assertEqual(bodies[1]["creative_list"][0]["creative_info"]["ad_format"], "SINGLE_VIDEO")
        self.assertEqual(bodies[0]["ad_name"], names[0])
        self.assertEqual(bodies[1]["ad_name"], names[1])
        self.assertNotEqual(bodies[0]["page_list"], bodies[1]["page_list"])


class LaunchShellTests(unittest.TestCase):
    """A launch that creates no ads must not leave a campaign shell behind."""

    @staticmethod
    def _request(calls, *, fail_path="", campaign="camp-1", adgroup="group-1"):
        def request(_method, path, _token=None, json_body=None, params=None, **_kwargs):
            calls.append({"path": path, "body": json_body, "params": params})
            if path == fail_path:
                raise TikTokApiError("request failed")
            if path == "/smart_plus/campaign/create/":
                return {"campaign_id": campaign}
            if path == "/smart_plus/adgroup/create/":
                return {"adgroup_id": adgroup}
            return {}

        return request

    @staticmethod
    def _deletes(calls):
        return [
            call
            for call in calls
            if str(call["path"]).endswith("/status/update/")
            and (call["body"] or {}).get("operation_status") == "DELETE"
        ]

    def test_no_videos_refuses_before_creating_a_campaign(self) -> None:
        from app.tiktok_marketing.launch import launch_for_account

        calls: list[dict] = []
        with (
            mock.patch("app.tiktok_marketing.launch.find_existing_campaign", return_value=None),
            mock.patch("app.tiktok_marketing.launch.card_mod.sales_settings", return_value={}),
            mock.patch("app.tiktok_marketing.launch.tiktok_request", side_effect=self._request(calls)),
        ):
            with self.assertRaises(TikTokApiError) as raised:
                launch_for_account(
                    "uid",
                    {"profileNo": "267"},
                    {"accountId": "adv"},
                    {"objective": "lead", "renders": []},
                    token="token",
                )
        self.assertIn("No TikTok videos", str(raised.exception))
        self.assertFalse(any(call["path"] == "/smart_plus/campaign/create/" for call in calls))

    def test_missing_page_returns_before_creating_a_campaign(self) -> None:
        from app.tiktok_marketing.launch import launch_for_account

        calls: list[dict] = []
        with (
            mock.patch("app.tiktok_marketing.launch.find_existing_campaign", return_value=None),
            mock.patch("app.tiktok_marketing.launch.card_mod.sales_settings", return_value={}),
            mock.patch("app.tiktok_marketing.launch.find_page", return_value=None),
            mock.patch("app.tiktok_marketing.launch.tiktok_request", side_effect=self._request(calls)),
        ):
            result = launch_for_account(
                "uid",
                {"profileNo": "267"},
                {"accountId": "adv"},
                {"objective": "lead"},
                token="token",
                posts=[{"itemId": "123"}],
            )
        self.assertEqual(result["apiLaunchStep"], "lead_form")
        self.assertEqual(result["tiktokCampaignId"], "")
        self.assertFalse(any(call["path"] == "/smart_plus/campaign/create/" for call in calls))

    def test_adgroup_failure_deletes_the_new_campaign(self) -> None:
        from app.tiktok_marketing.launch import launch_for_account

        calls: list[dict] = []
        with (
            mock.patch("app.tiktok_marketing.launch.find_existing_campaign", return_value=None),
            mock.patch("app.tiktok_marketing.launch.card_mod.sales_settings", return_value={}),
            mock.patch(
                "app.tiktok_marketing.launch.tiktok_request",
                side_effect=self._request(calls, fail_path="/smart_plus/adgroup/create/"),
            ),
        ):
            with self.assertRaises(TikTokApiError):
                launch_for_account(
                    "uid",
                    {"profileNo": "267"},
                    {"accountId": "adv"},
                    {"objective": "lead", "pageId": "page-1"},
                    token="token",
                    posts=[{"itemId": "123"}],
                )
        deletes = self._deletes(calls)
        self.assertEqual(len(deletes), 1)
        self.assertEqual(deletes[0]["path"], "/smart_plus/campaign/status/update/")
        self.assertEqual(deletes[0]["body"]["campaign_ids"], ["camp-1"])

    def test_zero_ads_deletes_the_adgroup_then_the_campaign(self) -> None:
        from app.tiktok_marketing.launch import launch_for_account

        calls: list[dict] = []
        with (
            mock.patch("app.tiktok_marketing.launch.find_existing_campaign", return_value=None),
            mock.patch("app.tiktok_marketing.launch.card_mod.sales_settings", return_value={}),
            mock.patch("app.tiktok_marketing.launch.tiktok_request", side_effect=self._request(calls)),
            mock.patch(
                "app.tiktok_marketing.launch.create_sales_ads",
                side_effect=TikTokApiError("No ads were created"),
            ),
        ):
            with self.assertRaises(TikTokApiError):
                launch_for_account(
                    "uid",
                    {"profileNo": "267"},
                    {"accountId": "adv"},
                    {"objective": "lead", "pageId": "page-1"},
                    token="token",
                    posts=[{"itemId": "123"}],
                )
        deletes = self._deletes(calls)
        self.assertEqual([call["path"] for call in deletes], [
            "/smart_plus/adgroup/status/update/",
            "/smart_plus/campaign/status/update/",
        ])
        self.assertEqual(deletes[0]["body"]["adgroup_ids"], ["group-1"])
        self.assertEqual(deletes[1]["body"]["campaign_ids"], ["camp-1"])

    def test_partial_ad_launch_is_not_deleted(self) -> None:
        from app.tiktok_marketing.launch import launch_for_account

        calls: list[dict] = []

        def create(_token, **kwargs):
            kwargs["progress"].append("ad-1")
            raise TikTokApiError("second ad failed")

        with (
            mock.patch("app.tiktok_marketing.launch.find_existing_campaign", return_value=None),
            mock.patch("app.tiktok_marketing.launch.card_mod.sales_settings", return_value={}),
            mock.patch("app.tiktok_marketing.launch.tiktok_request", side_effect=self._request(calls)),
            mock.patch("app.tiktok_marketing.launch.create_sales_ads", side_effect=create),
        ):
            with self.assertRaises(TikTokApiError):
                launch_for_account(
                    "uid",
                    {"profileNo": "267"},
                    {"accountId": "adv"},
                    {"objective": "lead", "pageId": "page-1"},
                    token="token",
                    posts=[{"itemId": "123"}],
                )
        self.assertEqual(self._deletes(calls), [])

    def test_regular_campaign_delete_falls_back_from_smart_plus(self) -> None:
        from app.tiktok_marketing.launch import delete_launch_shell

        calls: list[dict] = []

        def request(_method, path, _token=None, json_body=None, **_kwargs):
            calls.append({"path": path, "body": json_body})
            if path == "/smart_plus/campaign/status/update/":
                return {"campaign_list": [], "error_list": [{"error_message": "not SmartPlus"}]}
            return {"campaign_ids": ["regular-1"], "status": "DELETE"}

        with mock.patch("app.tiktok_marketing.launch.tiktok_request", side_effect=request):
            result = delete_launch_shell(
                "token",
                "adv",
                campaign_id="regular-1",
                adgroup_ids=[],
            )
        self.assertTrue(result["campaignDeleted"])
        self.assertEqual(
            [call["path"] for call in calls],
            ["/smart_plus/campaign/status/update/", "/campaign/status/update/"],
        )

    def test_reach_ad_failure_deletes_the_regular_campaign(self) -> None:
        from app.tiktok_marketing.whitehat_api import publish_reach

        calls: list[str] = []

        def request(_method, path, _token=None, json_body=None, **_kwargs):
            calls.append(path)
            if path == "/campaign/create/":
                return {"campaign_id": "reach-1"}
            if path == "/adgroup/create/":
                return {"adgroup_id": "reach-group-1"}
            if path == "/ad/create/":
                raise TikTokApiError("ad failed")
            if path.startswith("/smart_plus/"):
                return {"error_list": [{"error_message": "not SmartPlus"}]}
            return {"status": "DELETE"}

        with (
            mock.patch(
                "app.tiktok_marketing.whitehat_api.authorize_spark",
                return_value={"itemId": "item-1", "identityId": "identity-1", "identityType": "AUTH_CODE"},
            ),
            mock.patch("app.tiktok_marketing.whitehat_api.tiktok_request", side_effect=request),
            mock.patch("app.tiktok_marketing.launch.tiktok_request", side_effect=request),
        ):
            with self.assertRaises(TikTokApiError):
                publish_reach(
                    "token",
                    advertiser_id="adv",
                    name="whitehat-267-ac1",
                    spark_code="code",
                )
        self.assertIn("/adgroup/status/update/", calls)
        self.assertIn("/campaign/status/update/", calls)

    def test_posted_sales_page_failure_deletes_the_new_campaign(self) -> None:
        from app.tiktok_marketing.posted_sales import _launch

        calls: list[dict] = []

        def request(_method, path, _token=None, json_body=None, params=None, **_kwargs):
            calls.append({"path": path, "body": json_body, "params": params})
            if path == "/smart_plus/campaign/create/":
                return {"campaign_id": "camp-2"}
            if path == "/smart_plus/adgroup/create/":
                return {"adgroup_id": "group-2"}
            return {}

        def make_page(_landing):
            raise TikTokApiError("Instant Page was not published")

        with (
            mock.patch("app.tiktok_marketing.launch.find_existing_campaign", return_value=None),
            mock.patch("app.tiktok_marketing.posted_sales.card_mod.sales_settings", return_value={}),
            mock.patch("app.tiktok_marketing.posted_sales.tiktok_request", side_effect=request),
            mock.patch("app.tiktok_marketing.launch.tiktok_request", side_effect=request),
        ):
            with self.assertRaises(TikTokApiError):
                _launch(
                    "uid",
                    {"profileNo": "267"},
                    "token",
                    {"identity_id": "id"},
                    "adv",
                    make_page=make_page,
                    offer_name="Rico",
                    posts=[{"itemId": "123"}],
                    budget=20,
                    bid=0.2,
                )
        deletes = self._deletes(calls)
        self.assertEqual([call["path"] for call in deletes], [
            "/smart_plus/adgroup/status/update/",
            "/smart_plus/campaign/status/update/",
        ])
        self.assertEqual(deletes[0]["body"]["adgroup_ids"], ["group-2"])
        self.assertEqual(deletes[1]["body"]["campaign_ids"], ["camp-2"])


if __name__ == "__main__":
    unittest.main()
