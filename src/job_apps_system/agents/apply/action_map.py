from __future__ import annotations

from playwright.sync_api import Frame

from job_apps_system.schemas.apply import ApplyField


CONTROL_SELECTOR = "input, textarea, select"


def extract_apply_fields(frame: Frame, *, frame_id: str = "main") -> list[ApplyField]:
    rows = frame.locator(CONTROL_SELECTOR).evaluate_all(
        """
        els => els
          .filter((el) => {
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            const type = (el.getAttribute("type") || "").toLowerCase();
            return rect.width > 0 &&
              rect.height > 0 &&
              style.display !== "none" &&
              style.visibility !== "hidden" &&
              type !== "hidden" &&
              !el.disabled;
          })
          .map((el, index) => {
            const elementId = `el_${String(index + 1).padStart(3, "0")}`;
            el.setAttribute("data-apply-agent-id", elementId);

            const labels = [];
            if (el.labels) {
              Array.from(el.labels).forEach((label) => {
                const text = (label.innerText || label.textContent || "").trim();
                if (text) labels.push(text);
              });
            }
            const aria = el.getAttribute("aria-label") || "";
            if (aria) labels.push(aria);
            const placeholder = el.getAttribute("placeholder") || "";
            if (placeholder) labels.push(placeholder);

            let parent = el.parentElement;
            for (let depth = 0; parent && depth < 4; depth += 1, parent = parent.parentElement) {
              const text = (parent.innerText || parent.textContent || "").replace(/\\s+/g, " ").trim();
              if (text && text.length <= 500) {
                labels.push(text);
                break;
              }
            }

            return {
              element_id: elementId,
              tag: el.tagName.toLowerCase(),
              type: (el.getAttribute("type") || "").toLowerCase(),
              label: labels.join(" | ").slice(0, 500),
              placeholder,
              required: Boolean(el.required || el.getAttribute("aria-required") === "true"),
              selector: `[data-apply-agent-id="${elementId}"]`,
            };
          })
        """
    )
    fields: list[ApplyField] = []
    for row in rows:
        fields.append(ApplyField(frame_id=frame_id, **row))
    return fields


def normalized_text(value: str | None) -> str:
    return " ".join((value or "").lower().split())
