from __future__ import annotations

import logging
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page

from job_apps_system.agents.apply.ai_browser_loop import AiBrowserApplyLoop
from job_apps_system.config.models import ApplicantProfileConfig
from job_apps_system.db.models.jobs import Job
from job_apps_system.schemas.apply import ApplyJobResult
from job_apps_system.services.application_answer_service import ApplicationAnswerService
from job_apps_system.services.applicant_names import applicant_name_parts
from job_apps_system.services.apply_site_sessions import ApplySiteCredential


logger = logging.getLogger(__name__)


class OracleCloudApplyAdapter:
    ats_type = "oracle_cloud"

    def apply(
        self,
        *,
        page: Page,
        job: Job,
        applicant: ApplicantProfileConfig,
        resume_path: Path,
        answer_service: ApplicationAnswerService,
        screenshot_path: Path,
        auto_submit: bool,
        cancel_checker=None,
        site_credential: ApplySiteCredential | None = None,
    ) -> ApplyJobResult:
        steps: list[str] = []
        self._record_step(steps, "Oracle Cloud adapter started.")
        return AiBrowserApplyLoop(retain_success_logs=True).apply(
            page=page,
            job=job,
            applicant=applicant,
            resume_path=resume_path,
            answer_service=answer_service,
            screenshot_path=screenshot_path,
            auto_submit=auto_submit,
            detected_ats=self.ats_type,
            site_credential=site_credential,
            initial_reason="Oracle Cloud adapter delegated form completion to AI browser loop.",
            manual_resume_url=job.apply_url,
            cancel_checker=cancel_checker,
            before_observe_hook=lambda page, steps: self._before_observe(page, steps, applicant),
        )

    def _before_observe(self, page: Page, steps: list[str], applicant: ApplicantProfileConfig) -> bool:
        if self._accept_required_terms(page, steps):
            return True
        if self._answer_screening_questions(page, steps, applicant):
            return True
        return self._complete_voluntary_self_identification(page, steps, applicant)

    def _accept_required_terms(self, page: Page, steps: list[str]) -> bool:
        if not self._is_oracle_cloud_page(page):
            return False
        accepted = False
        for frame in page.frames:
            if not self._is_oracle_cloud_url(frame.url):
                continue
            try:
                result = frame.evaluate(ORACLE_TERMS_ACCEPT_SCRIPT)
            except PlaywrightError:
                continue
            if not isinstance(result, dict) or not result.get("accepted"):
                continue
            label = str(result.get("label") or "terms and conditions").strip()
            self._record_step(steps, f"Accepted Oracle Cloud required checkbox: {label}.")
            accepted = True
        if accepted:
            try:
                page.wait_for_timeout(500)
            except PlaywrightError:
                pass
        return accepted

    def _answer_screening_questions(self, page: Page, steps: list[str], applicant: ApplicantProfileConfig) -> bool:
        if not self._is_oracle_cloud_page(page):
            return False
        payload = {
            "adult": True,
            "workAuthorizedUs": bool(applicant.work_authorized_us),
            "requiresSponsorship": bool(applicant.requires_sponsorship),
        }
        answered: list[str] = []
        for frame in page.frames:
            if not self._is_oracle_cloud_url(frame.url):
                continue
            try:
                result = frame.evaluate(ORACLE_SCREENING_QUESTION_SCRIPT, payload)
            except PlaywrightError:
                continue
            if not isinstance(result, dict):
                continue
            for item in result.get("answered") or []:
                if isinstance(item, str) and item.strip():
                    answered.append(item.strip())
        if not answered:
            return False
        for question in answered[:4]:
            self._record_step(steps, f"Answered Oracle Cloud screening question: {question}.")
        try:
            page.wait_for_timeout(500)
        except PlaywrightError:
            pass
        return True

    def _complete_voluntary_self_identification(
        self,
        page: Page,
        steps: list[str],
        applicant: ApplicantProfileConfig,
    ) -> bool:
        if not self._is_oracle_cloud_page(page):
            return False
        names = applicant_name_parts(applicant)
        profile = {
            "fullName": names.full_name,
            "linkUrl": (
                applicant.linkedin_url
                or applicant.portfolio_url
                or applicant.github_url
                or ""
            ).strip(),
        }
        completed: list[str] = []
        for frame in page.frames:
            if not self._is_oracle_cloud_url(frame.url):
                continue
            try:
                result = frame.evaluate(ORACLE_VOLUNTARY_SELF_ID_SCRIPT, profile)
            except PlaywrightError:
                continue
            if not isinstance(result, dict):
                continue
            for item in result.get("completed") or []:
                if isinstance(item, str) and item.strip():
                    completed.append(item.strip())
        if not completed:
            return False
        for item in completed[:6]:
            self._record_step(steps, item)
        try:
            page.wait_for_timeout(700)
        except PlaywrightError:
            pass
        return True

    @staticmethod
    def _is_oracle_cloud_page(page: Page) -> bool:
        try:
            if OracleCloudApplyAdapter._is_oracle_cloud_url(page.url):
                return True
            return any(OracleCloudApplyAdapter._is_oracle_cloud_url(frame.url) for frame in page.frames)
        except PlaywrightError:
            return False

    @staticmethod
    def _is_oracle_cloud_url(url: str | None) -> bool:
        if not url:
            return False
        return "oraclecloud.com" in urlparse(url).netloc.lower()

    @staticmethod
    def _record_step(steps: list[str], message: str) -> None:
        steps.append(message)
        logger.info("Oracle Cloud apply step: %s", message)


ORACLE_TERMS_ACCEPT_SCRIPT = """
() => {
  const normalize = (value) => String(value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
  const isVisible = (el) => {
    if (!el || !(el instanceof Element)) return false;
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return rect.width > 0 &&
      rect.height > 0 &&
      style.display !== 'none' &&
      style.visibility !== 'hidden' &&
      style.opacity !== '0';
  };
  const checked = (el) => {
    if (!el) return false;
    if ('checked' in el) return Boolean(el.checked);
    return normalize(el.getAttribute('aria-checked')) === 'true';
  };
  const setChecked = (el) => {
    if (!el) return false;
    if ('checked' in el) {
      const proto = Object.getPrototypeOf(el);
      const descriptor = Object.getOwnPropertyDescriptor(proto, 'checked');
      if (descriptor && descriptor.set) {
        descriptor.set.call(el, true);
      } else {
        el.checked = true;
      }
    }
    el.setAttribute('aria-checked', 'true');
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
    return checked(el);
  };
  const termsTokens = [
    'terms and conditions',
    'terms of use',
    'privacy policy',
    'i agree',
    'i have read',
    'agree to the terms',
    'accept the terms',
    'consent'
  ];
  const isTermsText = (text) => {
    const normalized = normalize(text);
    return termsTokens.some((token) => normalized.includes(token));
  };
  const controls = Array.from(document.querySelectorAll([
    'input[type="checkbox"]',
    '[role="checkbox"]',
    'oj-checkboxset',
    'oj-option',
    '.oj-checkboxset',
    '.oj-choice-item',
    '.oj-checkbox-label',
    '[class*="checkbox" i]',
    '[class*="choice" i]',
    'label'
  ].join(',')));
  const candidates = [];
  for (const control of controls) {
    const labels = [];
    if (control.labels) {
      labels.push(...Array.from(control.labels).map((label) => label.innerText || label.textContent || ''));
    }
    labels.push(control.innerText || control.textContent || '');
    labels.push(control.getAttribute('aria-label') || '');
    labels.push(control.getAttribute('title') || '');
    const parent = control.closest('label, .oj-choice-item, .oj-checkboxset, [role="group"], [class*="terms" i], [class*="privacy" i]');
    if (parent && parent !== control) labels.push(parent.innerText || parent.textContent || '');
    const text = labels.filter(Boolean).join(' ');
    if (isTermsText(text)) candidates.push({ control, text });
  }
  for (const candidate of candidates) {
    const root = candidate.control;
    const input = root.matches('input[type="checkbox"], [role="checkbox"]')
      ? root
      : root.querySelector('input[type="checkbox"], [role="checkbox"]');
    const clickable = root.closest('label, .oj-choice-item, .oj-checkboxset, oj-checkboxset, oj-option') || root;
    if (input && checked(input)) {
      return { accepted: false, alreadyChecked: true, label: candidate.text.slice(0, 160) };
    }
    if (clickable && isVisible(clickable)) {
      try {
        clickable.click();
      } catch (_) {}
    }
    if (input && !checked(input)) {
      setChecked(input);
    }
    if (!input && root.getAttribute('role') === 'checkbox' && !checked(root)) {
      setChecked(root);
    }
    const accepted = input ? checked(input) : checked(root);
    if (accepted) {
      return { accepted: true, label: candidate.text.slice(0, 160) };
    }
  }
  return { accepted: false };
}
"""


ORACLE_SCREENING_QUESTION_SCRIPT = """
(answers) => {
  const normalize = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
  const lower = (value) => normalize(value).toLowerCase();
  const isVisible = (el) => {
    if (!el || !(el instanceof Element)) return false;
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return rect.width > 0 &&
      rect.height > 0 &&
      style.display !== 'none' &&
      style.visibility !== 'hidden' &&
      style.opacity !== '0' &&
      !el.disabled &&
      el.getAttribute('aria-disabled') !== 'true';
  };
  const controlText = (el) => lower(el.innerText || el.value || el.getAttribute('aria-label') || el.textContent || '');
  const isSelected = (el) => {
    const candidates = [el];
    const selectedAncestor = el.closest('[aria-pressed], [aria-checked], [aria-selected], .oj-selected, .oj-active, .selected, .is-selected');
    if (selectedAncestor) candidates.push(selectedAncestor);
    for (const candidate of candidates) {
      if (!candidate) continue;
      if (['aria-pressed', 'aria-checked', 'aria-selected'].some((attr) => lower(candidate.getAttribute(attr)) === 'true')) {
        return true;
      }
      const classes = lower(candidate.getAttribute('class') || '');
      if (classes.includes('oj-selected') || classes.includes('selected') || classes.includes('is-selected')) {
        return true;
      }
    }
    return false;
  };
  const answerForQuestion = (text) => {
    const question = lower(text);
    if (!question) return null;
    if ([
      'at least 18',
      '18 years of age',
      '18 years old',
      'over 18',
      'older than 18',
      'age of majority'
    ].some((token) => question.includes(token))) {
      return Boolean(answers.adult);
    }
    const asksAuthorization = [
      'legally authorized',
      'authorized to work',
      'eligible to work',
      'legal right to work',
      'right to work'
    ].some((token) => question.includes(token));
    const asksSponsorship = [
      'sponsorship',
      'sponsor',
      'visa',
      'immigration support',
      'immigration-related support',
      'employment-based'
    ].some((token) => question.includes(token));
    if (asksSponsorship) return Boolean(answers.requiresSponsorship);
    if (asksAuthorization) return Boolean(answers.workAuthorizedUs);
    if ([
      'previously employed',
      'prior employment',
      'worked for jpmorgan',
      'worked for jp morgan',
      'worked for chase',
      'former employee',
      'family member',
      'relative',
      'related to an employee',
      'close personal relationship',
      'government official',
      'politically exposed'
    ].some((token) => question.includes(token))) {
      return false;
    }
    return null;
  };
  const clickControl = (el) => {
    try {
      el.scrollIntoView({ block: 'center', inline: 'center' });
    } catch (_) {}
    try {
      el.click();
      return true;
    } catch (_) {
      return false;
    }
  };
  const allControls = Array.from(document.querySelectorAll([
    'button',
    '[role="button"]',
    'input[type="button"]',
    'input[type="submit"]',
    'oj-option',
    '.oj-button',
    '[class*="oj-button" i]'
  ].join(','))).filter(isVisible);
  const yesNoControls = allControls
    .map((control) => ({ control, text: controlText(control), rect: control.getBoundingClientRect() }))
    .filter((item) => item.text === 'yes' || item.text === 'no');
  if (yesNoControls.length < 2) {
    return { answered: [] };
  }

  const questionCandidates = Array.from(document.querySelectorAll([
    'label',
    'oj-label',
    '.oj-label',
    '[id]',
    '[class*="question" i]',
    'div',
    'span',
    'p'
  ].join(',')))
    .filter(isVisible)
    .map((el) => ({ el, text: normalize(el.innerText || el.textContent || ''), rect: el.getBoundingClientRect() }))
    .filter((item) => {
      const text = lower(item.text);
      return item.text.length >= 10 &&
        item.text.length <= 360 &&
        answerForQuestion(text) !== null;
    })
    .sort((a, b) => a.rect.top - b.rect.top || a.rect.left - b.rect.left);

  const usedControls = new Set();
  const usedQuestionBands = [];
  const usedQuestionText = new Set();
  const answered = [];
  for (const question of questionCandidates) {
    const expected = answerForQuestion(question.text);
    if (expected === null) continue;
    const questionKey = lower(question.text);
    if (usedQuestionText.has(questionKey) || usedQuestionBands.some((top) => Math.abs(top - question.rect.top) < 28)) {
      continue;
    }
    const desiredText = expected ? 'yes' : 'no';
    const nearest = yesNoControls
      .filter((item) => !usedControls.has(item.control) && item.text === desiredText)
      .filter((item) => item.rect.bottom >= question.rect.top - 8)
      .map((item) => {
        const yDistance = Math.max(0, item.rect.top - question.rect.top);
        const xDistance = Math.abs((item.rect.left + item.rect.right) / 2 - (question.rect.left + question.rect.right) / 2);
        return { item, score: yDistance + xDistance / 20 };
      })
      .filter((candidate) => candidate.score >= 0 && candidate.score < 520)
      .sort((a, b) => a.score - b.score)[0]?.item;
    if (!nearest) continue;
    if (isSelected(nearest.control)) {
      for (const item of yesNoControls) {
        if (Math.abs(item.rect.top - nearest.rect.top) < 20) {
          usedControls.add(item.control);
        }
      }
      usedQuestionText.add(questionKey);
      usedQuestionBands.push(question.rect.top);
      continue;
    }
    if (clickControl(nearest.control)) {
      for (const item of yesNoControls) {
        if (Math.abs(item.rect.top - nearest.rect.top) < 20) {
          usedControls.add(item.control);
        }
      }
      usedQuestionText.add(questionKey);
      usedQuestionBands.push(question.rect.top);
      answered.push(`${question.text.slice(0, 120)} => ${desiredText.toUpperCase()}`);
    }
  }

  if (answered.length === 0) {
    const controlsByRow = yesNoControls
      .slice()
      .sort((a, b) => a.rect.top - b.rect.top || a.rect.left - b.rect.left);
    const pageText = lower(document.body?.innerText || '');
    const inferredAnswers = [];
    if (pageText.includes('18 years')) inferredAnswers.push(Boolean(answers.adult));
    if (pageText.includes('authorized to work') || pageText.includes('legally authorized')) inferredAnswers.push(Boolean(answers.workAuthorizedUs));
    if (pageText.includes('sponsorship') || pageText.includes('sponsor')) inferredAnswers.push(Boolean(answers.requiresSponsorship));
    for (let index = 0; index < inferredAnswers.length; index += 1) {
      const desiredText = inferredAnswers[index] ? 'yes' : 'no';
      const rowControls = controlsByRow.slice(index * 2, index * 2 + 2);
      const match = rowControls.find((item) => item.text === desiredText);
      if (match && isSelected(match.control)) {
        continue;
      }
      if (match && clickControl(match.control)) {
        answered.push(`Oracle screening question ${index + 1} => ${desiredText.toUpperCase()}`);
      }
    }
  }

  return { answered };
}
"""


ORACLE_VOLUNTARY_SELF_ID_SCRIPT = """
async (profile) => {
  const sleep = (ms) => new Promise((resolve) => window.setTimeout(resolve, ms));
  const normalize = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
  const lower = (value) => normalize(value).toLowerCase();
  const bodyText = lower(document.body?.innerText || '');
  if (!bodyText.includes('military status') &&
      !bodyText.includes('veteran status') &&
      !bodyText.includes('voluntary self-identification')) {
    return { completed: [] };
  }

  const completed = [];
  const declineChoices = [
    'I do not wish to answer',
    "I don't wish to answer",
    'I do not want to answer',
    'Prefer not to answer',
    'Decline To Self Identify'
  ];
  const genderChoices = declineChoices;
  const militarySpouseChoices = [
    'No',
    'Not Applicable',
    ...declineChoices
  ];
  const militaryStatusChoices = [
    'I do not wish to answer',
    "I don't wish to answer",
    'I do not want to answer',
    'Not a Veteran',
    'No Military Service',
    'Not Applicable',
    'Prefer not to answer'
  ];
  const veteranChoices = [
    'I do not wish to answer',
    "I don't wish to answer",
    'I do not want to answer',
    'I am not a protected veteran',
    'Not a Veteran',
    'Prefer not to answer'
  ];

  const isVisible = (el) => {
    if (!el || !(el instanceof Element)) return false;
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return rect.width > 0 &&
      rect.height > 0 &&
      style.display !== 'none' &&
      style.visibility !== 'hidden' &&
      style.opacity !== '0' &&
      !el.disabled &&
      el.getAttribute('aria-disabled') !== 'true';
  };
  const dispatchInput = (el) => {
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
    el.dispatchEvent(new Event('blur', { bubbles: true }));
  };
  const clickNode = (el) => {
    try {
      el.scrollIntoView({ block: 'center', inline: 'center' });
    } catch (_) {}
    try {
      el.click();
      return true;
    } catch (_) {
      return false;
    }
  };
  const valueOf = (el) => {
    if (!el) return '';
    if ('value' in el) return normalize(el.value);
    const input = el.querySelector?.('input, [role="combobox"], [aria-valuetext]');
    if (input) {
      return normalize(input.value || input.getAttribute('aria-valuetext') || input.textContent || '');
    }
    return normalize(el.getAttribute?.('aria-valuetext') || el.textContent || '');
  };
  const optionMatches = (text, candidates) => {
    const normalized = lower(text);
    return candidates.some((candidate) => {
      const expected = lower(candidate);
      if (normalized === expected) return true;
      if (expected.length <= 2 || normalized.length <= 2) return false;
      return normalized.includes(expected) || expected.includes(normalized);
    });
  };
  const choiceAlreadySelected = (control, candidates) => {
    const value = valueOf(control);
    return value && optionMatches(value, candidates);
  };
  const findOpenButton = (label) => {
    const expected = `open the drop-down list for ${lower(label)}`;
    return Array.from(document.querySelectorAll('button, [role="button"], a'))
      .filter(isVisible)
      .find((el) => lower(el.getAttribute('aria-label') || el.textContent || '').includes(expected));
  };
  const findLabel = (label) => {
    const expected = lower(label);
    return Array.from(document.querySelectorAll('label, oj-label, .oj-label, span, div'))
      .filter(isVisible)
      .map((el) => ({ el, text: lower(el.innerText || el.textContent || ''), rect: el.getBoundingClientRect() }))
      .filter((item) => item.text === expected || item.text.startsWith(`${expected} *`) || item.text.startsWith(`${expected}:`))
      .sort((a, b) => a.text.length - b.text.length || a.rect.top - b.rect.top)[0]?.el || null;
  };
  const findControlForLabel = (label) => {
    const button = findOpenButton(label);
    if (button) {
      return button.closest('oj-select-single, oj-select-one, oj-combobox-one, [role="combobox"], .oj-select, .oj-combobox')
        || button.parentElement
        || button;
    }
    const labelEl = findLabel(label);
    if (!labelEl) return null;
    const labelRect = labelEl.getBoundingClientRect();
    return Array.from(document.querySelectorAll('input, textarea, select, [role="combobox"], oj-select-single, oj-select-one, oj-combobox-one'))
      .filter(isVisible)
      .map((el) => {
        const rect = el.getBoundingClientRect();
        const vertical = rect.top - labelRect.bottom;
        const horizontalOverlap = Math.min(rect.right, labelRect.right + 520) - Math.max(rect.left, labelRect.left - 40);
        return { el, rect, score: Math.abs(vertical) + Math.abs(rect.left - labelRect.left) / 20, vertical, horizontalOverlap };
      })
      .filter((item) => item.vertical >= -8 && item.vertical < 150 && item.horizontalOverlap > 0)
      .sort((a, b) => a.score - b.score)[0]?.el || null;
  };
  const selectNativeOption = (select, candidates) => {
    for (const option of Array.from(select.options || [])) {
      if (!optionMatches(option.textContent || option.label || option.value, candidates)) continue;
      if (select.value === option.value) return false;
      select.value = option.value;
      dispatchInput(select);
      return true;
    }
    return false;
  };
  const findVisibleOption = (candidates) => {
    const options = Array.from(document.querySelectorAll([
      '[role="option"]',
      'oj-option',
      'li',
      '.oj-listbox-option',
      '.oj-listbox-result',
      '[class*="oj-listbox" i] [class*="option" i]'
    ].join(',')))
      .filter(isVisible)
      .map((el) => ({ el, text: normalize(el.innerText || el.textContent || ''), rect: el.getBoundingClientRect() }))
      .filter((item) => item.text && optionMatches(item.text, candidates));
    return options
      .sort((a, b) => {
        const aExact = candidates.some((candidate) => lower(a.text) === lower(candidate)) ? 0 : 1;
        const bExact = candidates.some((candidate) => lower(b.text) === lower(candidate)) ? 0 : 1;
        return aExact - bExact || a.text.length - b.text.length || a.rect.top - b.rect.top;
      })[0] || null;
  };
  const selectDropdown = async (label, candidates) => {
    const control = findControlForLabel(label);
    if (!control || choiceAlreadySelected(control, candidates)) return false;
    if (control.tagName?.toLowerCase() === 'select') {
      return selectNativeOption(control, candidates);
    }
    const opener = findOpenButton(label) || control.querySelector?.('button, [role="button"], input, [role="combobox"]') || control;
    if (!clickNode(opener)) return false;
    await sleep(300);
    const option = findVisibleOption(candidates);
    if (!option) return false;
    clickNode(option.el);
    await sleep(250);
    return true;
  };
  const setTextField = (label, value, { optionalUrl = false } = {}) => {
    const control = findControlForLabel(label);
    if (!control || !('value' in control)) return false;
    const current = normalize(control.value);
    const nextValue = normalize(value);
    if (optionalUrl) {
      const currentLooksLikeUrl = /^https?:\\/\\//i.test(current);
      if (!nextValue && (!current || currentLooksLikeUrl)) return false;
      if (!nextValue && current && !currentLooksLikeUrl) {
        control.value = '';
        dispatchInput(control);
        return true;
      }
    }
    if (!nextValue || current === nextValue) return false;
    const currentWrongType = /@|\\d{3}|\\(\\d{3}\\)|^\\+?1\\b/.test(current) || (optionalUrl && !/^https?:\\/\\//i.test(current));
    if (!current || currentWrongType) {
      control.value = nextValue;
      dispatchInput(control);
      return true;
    }
    return false;
  };
  const isRadioChecked = (node) => {
    if (!node) return false;
    if ('checked' in node) return Boolean(node.checked);
    return lower(node.getAttribute('aria-checked')) === 'true';
  };
  const selectRadioByText = (candidates) => {
    const controls = Array.from(document.querySelectorAll('input[type="radio"], [role="radio"]')).filter(isVisible);
    for (const control of controls) {
      let text = '';
      if (control.labels?.length) {
        text = Array.from(control.labels).map((label) => label.innerText || label.textContent || '').join(' ');
      }
      const container = control.closest('label, div, li, .oj-choice-item') || control.parentElement;
      text = normalize(`${text} ${container?.innerText || container?.textContent || ''}`);
      if (!optionMatches(text, candidates)) continue;
      if (isRadioChecked(control)) return false;
      return clickNode(container || control);
    }
    return false;
  };

  if (setTextField('Full Name', profile.fullName || '')) {
    completed.push('Corrected Oracle Cloud Full Name.');
  }
  if (setTextField('Link 1', profile.linkUrl || '', { optionalUrl: true })) {
    completed.push(profile.linkUrl ? 'Filled Oracle Cloud Link 1.' : 'Cleared invalid Oracle Cloud Link 1 value.');
  }
  if (selectRadioByText(['I do not want to answer', 'I do not wish to answer'])) {
    completed.push('Selected Oracle Cloud disability self-identification decline option.');
  }
  if (await selectDropdown('Gender', genderChoices)) {
    completed.push('Selected Oracle Cloud Gender: prefer not to answer.');
  }
  if (await selectDropdown('Military Spouse/Domestic Partner', militarySpouseChoices)) {
    completed.push('Selected Oracle Cloud Military Spouse/Domestic Partner.');
  }
  if (await selectDropdown('Military Status', militaryStatusChoices)) {
    completed.push('Selected Oracle Cloud Military Status.');
  }
  if (await selectDropdown('Veteran Status', veteranChoices)) {
    completed.push('Selected Oracle Cloud Veteran Status.');
  }

  return { completed };
}
"""


def is_oracle_cloud_page(page: Page) -> bool:
    return OracleCloudApplyAdapter._is_oracle_cloud_page(page)


def is_oracle_cloud_url(url: str | None) -> bool:
    return OracleCloudApplyAdapter._is_oracle_cloud_url(url)


__all__ = ["OracleCloudApplyAdapter", "is_oracle_cloud_page", "is_oracle_cloud_url"]
