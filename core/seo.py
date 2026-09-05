#!/usr/bin/env python3
"""SEO helpers for the Treat Motivated Capital blog.

Ports dexter's ``blog_bot/seo_optimizer.py`` into agent-trade, replacing the
bespoke Gemini call with the shared ``core.llm_client.SharedLLMClient`` transport
and reading WP auth via ``core.wordpress``.

Surface:
    SEOAuditor.audit_html(html) -> dict
    SEOAuditor.generate_seo_metadata(title, content) -> dict | None
"""

from __future__ import annotations

import json
import logging
import re

from core.llm_client import SharedLLMClient

logger = logging.getLogger("SEO")

# Lazy shared client
_client: SharedLLMClient | None = None
_client_attempted = False


def _get_client() -> SharedLLMClient | None:
    global _client, _client_attempted
    if _client_attempted:
        return _client
    _client_attempted = True
    try:
        _client = SharedLLMClient()
    except Exception as e:
        logger.error("SEO: failed to init SharedLLMClient: %s", e)
        _client = None
    return _client


class SEOAuditor:
    @staticmethod
    def audit_html(html_content: str) -> dict:
        """Static HTML audit for classic SEO rules (H1 count, image alt, links)."""
        results = {
            "passed": True,
            "warnings": [],
            "stats": {
                "h1_count": 0, "h2_count": 0,
                "images_total": 0, "images_missing_alt": 0,
                "links_total": 0, "links_external": 0,
            },
        }
        h1_tags = re.findall(r"<h1[^>]*>(.*?)</h1>", html_content or "", re.IGNORECASE)
        h2_tags = re.findall(r"<h2[^>]*>(.*?)</h2>", html_content or "", re.IGNORECASE)
        results["stats"]["h1_count"] = len(h1_tags)
        results["stats"]["h2_count"] = len(h2_tags)
        if len(h1_tags) > 1:
            results["warnings"].append(
                "[WARNING] Multiple <h1> tags found. Keep exactly one <h1> for SEO hierarchy.")
            results["passed"] = False

        img_tags = re.findall(r"<img([^>]+)>", html_content or "", re.IGNORECASE)
        results["stats"]["images_total"] = len(img_tags)
        for img in img_tags:
            alt_match = re.search(r"alt=[\"'](.*?)[\"']", img, re.IGNORECASE)
            if not alt_match or not alt_match.group(1).strip():
                results["stats"]["images_missing_alt"] += 1
        if results["stats"]["images_missing_alt"] > 0:
            results["warnings"].append(
                f"[WARNING] {results['stats']['images_missing_alt']} image(s) missing descriptive alt tags.")
            results["passed"] = False

        links = re.findall(r"<a[^>]+href=[\"'](.*?)[\"'][^>]*>", html_content or "", re.IGNORECASE)
        results["stats"]["links_total"] = len(links)
        for link in links:
            if "treatmotivated.capital" not in link and link.startswith("http"):
                results["stats"]["links_external"] += 1
        if not links:
            results["warnings"].append(
                "[LIGHT_BULB] Adding internal links helps Google discover your content.")
        return results

    @staticmethod
    def generate_seo_metadata(post_title: str, post_content: str) -> dict | None:
        """Generate SEO meta title/description/tags/JSON-LD via the shared LLM client.

        Returns a dict with keys ``meta_title``, ``meta_description``, ``tags``,
        ``json_ld_schema`` — or None if unavailable.
        """
        client = _get_client()
        if client is None:
            return None

        prompt = (
            "Analyze the following blog post title and content.\n"
            f"Title: {post_title}\n"
            f"Content Snippet: {post_content[:2000]}\n\n"
            "Generate the following SEO elements:\n"
            "1. Meta Title (Max 60 chars, engaging, in the 'Dexter the Dog Trader' blog persona).\n"
            "2. Meta Description (Max 155-160 chars, enticing).\n"
            "3. A list of 3-5 focus tags/keywords.\n"
            "4. A JSON-LD schema of type 'BlogPosting'.\n\n"
            'Return EXACTLY JSON with keys: "meta_title", "meta_description", "tags", "json_ld_schema".'
        )

        try:
            raw = client._execute_completion(
                prompt=prompt,
                system_prompt=(
                    "You are an SEO specialist for a public trading blog. "
                    "Never reference internal systems. Output only valid JSON."
                ),
                tier="utility",
                max_output_tokens=1024,
            )
            # Tolerate a JSON block inside code fences.
            text = raw.strip()
            # Tolerate code fences and any prose wrapper: extract the first
            # balanced {...} JSON object, which is robust to trailing text the
            # LLM occasionally appends after the JSON.
            if text.startswith("```"):
                text = re.sub(r"^```(?:json)?\s*", "", text).strip()
                text = re.sub(r"\s*```$", "", text).strip()
            obj = _extract_json_object(text)
            if obj is None:
                logger.warning("No valid JSON object found in SEO response.")
                return None
            return obj
        except Exception as e:
            logger.error("SEO metadata generation failed: %s", e)
            return None


def _extract_json_object(text: str) -> dict | None:
    """Return the first balanced JSON object in ``text``, or None."""
    text = (text or "").strip()
    for start in range(len(text)):
        if text[start] != "{":
            continue
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except Exception:
                        return None
    return None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    r = SEOAuditor.audit_html("<h1>Hi</h1><img src='x.png'><a href='https://treatmotivated.capital/tag/aapl/'>AAPL</a>")
    print("audit:", r)
    print("meta (may be None without keys):", SEOAuditor.generate_seo_metadata("Test", "<p>hello</p>"))