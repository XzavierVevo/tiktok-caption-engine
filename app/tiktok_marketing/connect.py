"""Sign a Business Center into the Marketing API inside its AdsPower profile.

Runs before white hat. Uses the same Hoot inbox code as the Ads login.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import parse_qs, urlparse

from ..provision.login import sign_in_if_prompted
from .client import TikTokApiError
from .launch import token_for_advertiser
from .oauth import build_auth_url, exchange_auth_code, list_authorized_advertisers, new_state
from .store import save_connection, save_oauth_state, mirror_connections

_log = logging.getLogger(__name__)


class MarketingConnectError(RuntimeError):
    """Marketing API authorization did not finish."""


def _body_text(page) -> str:
    try:
        return page.evaluate(
            "() => (document.body && document.body.innerText || '').replace(/\\s+/g, ' ')"
        ) or ""
    except Exception:
        return ""


def _identity_check_up(page) -> bool:
    return "verify your identity" in _body_text(page).lower()


def _capture_redirect(page) -> dict[str, str]:
    """Watch only the Caption Tool OAuth callback. Do not intercept every request."""
    caught = {"url": ""}

    def _maybe(url: str) -> None:
        if url and "auth_code=" in url:
            caught["url"] = url

    def _catch(route):
        req_url = route.request.url
        _maybe(req_url)
        if "oauth/tiktok" in req_url and "auth_code=" in req_url:
            route.abort()
            return
        route.continue_()

    page.route("**/*oauth/tiktok*", _catch)
    page.on("request", lambda req: _maybe(req.url))
    page.on("framenavigated", lambda frame: _maybe(getattr(frame, "url", "") or ""))
    return caught


def _auth_code_from_page(page, caught: dict[str, str]) -> str:
    urls = [caught.get("url") or "", getattr(page, "url", "") or ""]
    try:
        urls.extend(frame.url or "" for frame in page.frames)
    except Exception:
        pass
    for url in urls:
        code = _auth_code_from(url)
        if code:
            return code
    return ""


def _confirm_permissions(page) -> None:
    """Click the permissions Confirm. Leave the required box as TikTok set it."""
    clicked = page.evaluate(
        """() => {
          const buttons = [...document.querySelectorAll('button, [role="button"]')];
          const hits = buttons.filter((el) => {
            const t = ((el.innerText || '') + '').replace(/\\s+/g, ' ').trim();
            if (t !== 'Confirm') return false;
            const r = el.getBoundingClientRect();
            return r.width >= 40 && r.height >= 16;
          });
          const el = hits[hits.length - 1];
          if (!el) return false;
          el.click();
          return true;
        }"""
    )
    if not clicked:
        page.get_by_role("button", name="Confirm").last.click(timeout=8_000)


def _fill_identity_code(page, code: str) -> None:
    """Type into the empty code box. The email box uses the same placeholder."""
    fields = page.get_by_placeholder("Verification Code")
    target = None
    for i in range(fields.count()):
        field = fields.nth(i)
        try:
            if field.is_visible() and field.is_enabled():
                target = field
        except Exception:
            continue
    if target is None:
        raise MarketingConnectError("Verification code field was not editable")
    target.click(timeout=5_000)
    target.fill(code, timeout=5_000)
    confirmed = page.evaluate(
        """() => {
          const nodes = [...document.querySelectorAll('div, section, [role="dialog"]')];
          const hosts = nodes.filter((el) => {
            const t = ((el.innerText || '') + '').replace(/\\s+/g, ' ').toLowerCase();
            if (!t.includes('verify your identity')) return false;
            const r = el.getBoundingClientRect();
            return r.width >= 280 && r.width <= 720 && r.height >= 180 && r.height <= 640;
          });
          hosts.sort((a, b) => {
            const ra = a.getBoundingClientRect();
            const rb = b.getBoundingClientRect();
            return (ra.width * ra.height) - (rb.width * rb.height);
          });
          const host = hosts[0];
          if (!host) return false;
          for (const el of host.querySelectorAll('button, [role="button"]')) {
            const t = ((el.innerText || '') + '').replace(/\\s+/g, ' ').trim();
            if (t !== 'Confirm' || el.disabled) continue;
            el.click();
            return true;
          }
          return false;
        }"""
    )
    if not confirmed:
        page.get_by_role("button", name="Confirm").first.click(timeout=8_000)


def _click_send_code(page) -> bool:
    """Click the Send Code control inside Verify Your Identity."""
    clicked = page.evaluate(
        """() => {
          const nodes = [...document.querySelectorAll('button, a, span, div')];
          const hits = nodes.filter((el) => {
            const t = ((el.innerText || '') + '').replace(/\\s+/g, ' ').trim();
            if (t !== 'Send Code') return false;
            const r = el.getBoundingClientRect();
            return r.width >= 20 && r.height >= 10 && r.width < 220 && r.height < 80;
          });
          hits.sort((a, b) => {
            const ra = a.getBoundingClientRect();
            const rb = b.getBoundingClientRect();
            return (ra.width * ra.height) - (rb.width * rb.height);
          });
          const el = hits[0];
          if (!el) return false;
          el.click();
          return true;
        }"""
    )
    if clicked:
        return True
    try:
        page.get_by_text("Send Code", exact=True).click(timeout=5_000)
        return True
    except Exception:
        return False


def _hoot_code(
    profile: dict[str, Any],
    *,
    after_timestamp: int,
    exclude_code: str,
    timeout_s: float,
) -> str:
    from ..provision.hoot import HootError, poll_verification_code

    try:
        return poll_verification_code(
            account_id=str(profile.get("hootAccountId") or ""),
            email=str(profile.get("email") or ""),
            timeout_s=timeout_s,
            exclude_code=exclude_code,
            after_timestamp=after_timestamp,
        )
    except HootError:
        return ""


def _solve_oauth_captcha(page) -> None:
    """Same-shape captcha sits on top of Send Code until it is solved."""
    from ..provision.login_selectors import TwoCaptchaBalanceError
    from ..provision.same_shape_captcha import (
        captcha_visible,
        dismiss_verification_timeout,
        solve_same_shape_captcha,
        verification_timed_out,
    )

    if verification_timed_out(page):
        dismiss_verification_timeout(page)
    if not captcha_visible(page):
        return
    _log.info("Solving the same-shape captcha on the Marketing API sign-in")
    try:
        solve_same_shape_captcha(page)
    except TwoCaptchaBalanceError as exc:
        raise MarketingConnectError(str(exc)) from exc


def _wait_for_oauth_captcha(page) -> None:
    from ..provision.same_shape_captcha import captcha_visible, verification_timed_out

    for _ in range(16):
        if captcha_visible(page) or verification_timed_out(page):
            _solve_oauth_captcha(page)
            return
        page.wait_for_timeout(400)


def _complete_identity_check(page, profile: dict[str, Any]) -> None:
    """Click Send Code, then type the same Hoot verification code used for Ads login.

    TikTok often ignores the first send. Try Send Code up to three times.
    """
    from ..provision.hoot import fetch_verification_code

    hoot_id = str(profile.get("hootAccountId") or "")
    email = str(profile.get("email") or "")
    prior_code = ""
    prior_ts = 0
    if hoot_id or email:
        prior = fetch_verification_code(account_id=hoot_id, email=email)
        prior_code = str(prior.get("code") or "") if prior.get("found") else ""
        try:
            prior_ts = int(prior.get("timestamp") or 0)
        except (TypeError, ValueError):
            prior_ts = 0
    from ..provision.same_shape_captcha import captcha_visible, verification_timed_out

    code = ""
    for attempt in range(1, 4):
        if captcha_visible(page) or verification_timed_out(page):
            _solve_oauth_captcha(page)
        else:
            if not _click_send_code(page):
                raise MarketingConnectError("Send Code was not on the identity check")
            _log.info("Clicked Send Code (%s/3) for the Marketing API identity check", attempt)
            _wait_for_oauth_captcha(page)
        code = _hoot_code(
            profile,
            after_timestamp=prior_ts,
            exclude_code=prior_code,
            timeout_s=40,
        )
        if code:
            break
        _log.warning("Hoot had no new code after Send Code %s/3", attempt)
    if not code:
        raise MarketingConnectError(
            "Clicked Send Code 3 times and the Hoot inbox still had no verification code"
        )
    _fill_identity_code(page, code)


def _auth_code_from(url: str) -> str:
    query = parse_qs(urlparse(url).query)
    return str((query.get("auth_code") or query.get("code") or [""])[0])


def _persist_token(
    uid: str,
    profile: dict[str, Any],
    token: dict[str, Any],
) -> dict[str, Any]:
    profile_id = str(profile.get("id") or "")
    advertiser_ids = [str(x) for x in (token.get("advertiserIds") or []) if str(x).strip()]
    access = str(token.get("accessToken") or "")
    if access:
        try:
            listed = list_authorized_advertisers(access)
            extra = [
                str(row.get("advertiser_id") or row.get("advertiserId") or "").strip()
                for row in listed
                if isinstance(row, dict)
            ]
            advertiser_ids = [item for item in extra if item] or advertiser_ids
        except Exception:
            _log.warning("Could not list Marketing API advertisers after token exchange", exc_info=True)
    save_connection(
        uid,
        profile_id,
        {
            "accessToken": access,
            "advertiserIds": advertiser_ids,
            "oauthStatus": "connected",
            "scope": token.get("scope") or [],
            "bcId": str(profile.get("hootOrgId") or ""),
            "adspowerProfileId": str(profile.get("adspowerProfileId") or ""),
            "lastError": "",
        },
    )
    mirror_connections(uid)
    return {
        "oauthStatus": "connected",
        "profileId": profile_id,
        "advertiserIds": advertiser_ids,
    }


def _run_oauth_page(page, caught: dict[str, str], profile: dict[str, Any], *, uid: str) -> dict[str, Any]:
    profile_id = str(profile.get("id") or "")
    state = f"{uid}.{new_state()}"
    save_oauth_state(uid, profile_id, state)
    page.goto(build_auth_url(state), wait_until="domcontentloaded", timeout=90_000)
    page.wait_for_timeout(1500)
    sign_in_if_prompted(
        page,
        email=str(profile.get("email") or ""),
        password=str(profile.get("password") or ""),
        hoot_id=str(profile.get("hootAccountId") or ""),
        profile_id=profile_id,
    )
    body = _body_text(page).lower()
    if "requesting permission" in body or "confirm" in body:
        try:
            _confirm_permissions(page)
        except Exception:
            _log.warning("Permissions Confirm click missed; continuing", exc_info=True)
        for _ in range(24):
            if _identity_check_up(page) or _auth_code_from_page(page, caught):
                break
            page.wait_for_timeout(500)
    if _identity_check_up(page):
        _complete_identity_check(page, profile)
    try:
        page.wait_for_url("**auth_code=**", timeout=45_000)
    except Exception:
        page.wait_for_timeout(2000)
    code = _auth_code_from_page(page, caught)
    if not code:
        snippet = _body_text(page)[:240]
        raise MarketingConnectError(
            "Marketing API sign-in did not return an authorization code. "
            f"Page said: {snippet}"
        )
    try:
        token = exchange_auth_code(code)
    except TikTokApiError as exc:
        raise MarketingConnectError(f"Marketing API token exchange failed: {exc}") from exc
    if not token.get("accessToken"):
        raise MarketingConnectError("TikTok did not return a Marketing API token")
    result = _persist_token(uid, profile, token)
    _log.info(
        "Marketing API connected profile=%s advertisers=%s",
        profile_id[:8],
        len(result.get("advertiserIds") or []),
    )
    return result


def connect_marketing_api(context, profile: dict[str, Any], *, uid: str) -> dict[str, Any]:
    """Authorize Caption Tool for this profile. Returns when the token is saved.

    Opens the Marketing API consent page in the profile's own browser, confirms
    the permissions, and finishes the email check with the Hoot login code.
    """
    profile_id = str(profile.get("id") or "")
    if token_for_advertiser(uid, profile_id, ""):
        return {"oauthStatus": "connected", "profileId": profile_id, "already": True}

    page = context.new_page()
    caught = _capture_redirect(page)
    last_error = "Marketing API sign-in did not connect"
    try:
        for attempt in range(1, 4):
            try:
                return _run_oauth_page(page, caught, profile, uid=uid)
            except MarketingConnectError as exc:
                last_error = str(exc)
                _log.warning("Marketing API sign-in attempt %s/3 failed: %s", attempt, exc)
                save_connection(uid, profile_id, {"lastError": last_error, "oauthStatus": "pending"})
                page.wait_for_timeout(1500)
        raise MarketingConnectError(last_error)
    finally:
        try:
            page.close()
        except Exception:
            pass
