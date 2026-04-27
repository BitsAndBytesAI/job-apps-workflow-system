/* Motion module — Lenis smooth scroll + GSAP scroll-driven fades +
   magnetic cursor on .magnetic + Lottie player for workflow icons.
   All deps loaded from /static/uzhykova/lib/*. */

(function () {
  const ready = (fn) =>
    document.readyState === "loading"
      ? document.addEventListener("DOMContentLoaded", fn)
      : fn();

  ready(() => {
    initLenis();
    initGsapReveals();
    initMagnetic();
    initLottieIcons();
  });

  // --- Lenis smooth scroll ----------------------------------------
  function initLenis() {
    if (typeof window.Lenis !== "function") return;
    const lenis = new window.Lenis({
      duration: 1.1,
      easing: (t) => Math.min(1, 1.001 - Math.pow(2, -10 * t)),
      wheelMultiplier: 1,
      smoothWheel: true,
      smoothTouch: false,
    });
    function raf(time) {
      lenis.raf(time);
      requestAnimationFrame(raf);
    }
    requestAnimationFrame(raf);
    window.__lenis = lenis;
    if (window.gsap && window.ScrollTrigger) {
      lenis.on("scroll", window.ScrollTrigger.update);
      window.gsap.ticker.add((t) => lenis.raf(t * 1000));
      window.gsap.ticker.lagSmoothing(0);
    }
  }

  // --- GSAP scroll reveals ----------------------------------------
  function initGsapReveals() {
    if (!window.gsap) return;
    if (window.ScrollTrigger) window.gsap.registerPlugin(window.ScrollTrigger);

    // Stagger workflow cards on entry
    const cards = document.querySelectorAll(".dashboard-workflow-card");
    if (cards.length) {
      window.gsap.from(cards, {
        opacity: 0,
        y: 28,
        scale: 0.96,
        duration: 0.9,
        ease: "power3.out",
        stagger: 0.12,
        delay: 0.15,
      });
    }

    // Page header
    const header = document.querySelector(".page-header");
    if (header) {
      window.gsap.from(header, {
        opacity: 0,
        y: 18,
        duration: 0.8,
        ease: "power3.out",
      });
    }

    // Stat overview cards — slight stagger from the left
    const overview = document.querySelectorAll(".dashboard-overview-card");
    if (overview.length) {
      window.gsap.from(overview, {
        opacity: 0,
        y: 14,
        duration: 0.7,
        ease: "power3.out",
        stagger: 0.08,
        delay: 0.05,
      });
    }

    // Generic .animate-in scroll reveal for downstream pages
    if (window.ScrollTrigger) {
      document.querySelectorAll(".animate-in").forEach((el) => {
        if (el.classList.contains("page-header")) return; // already handled
        window.gsap.from(el, {
          opacity: 0,
          y: 22,
          duration: 0.8,
          ease: "power3.out",
          scrollTrigger: { trigger: el, start: "top 88%" },
        });
      });
    }
  }

  // --- Magnetic cursor effect -------------------------------------
  function initMagnetic() {
    const targets = document.querySelectorAll(
      ".magnetic, .app-tab, .app-utility-link, .btn-primary, .btn-accent"
    );
    targets.forEach((el) => {
      const strength = el.classList.contains("dashboard-workflow-card") ? 0.18 : 0.32;
      let raf;
      el.addEventListener("mousemove", (e) => {
        const r = el.getBoundingClientRect();
        const dx = (e.clientX - (r.left + r.width / 2)) * strength;
        const dy = (e.clientY - (r.top + r.height / 2)) * strength;
        cancelAnimationFrame(raf);
        raf = requestAnimationFrame(() => {
          if (window.gsap) {
            window.gsap.to(el, { x: dx, y: dy, duration: 0.4, ease: "power3.out" });
          } else {
            el.style.transform = `translate(${dx}px, ${dy}px)`;
          }
        });
      });
      el.addEventListener("mouseleave", () => {
        cancelAnimationFrame(raf);
        if (window.gsap) {
          window.gsap.to(el, { x: 0, y: 0, duration: 0.6, ease: "elastic.out(1, 0.4)" });
        } else {
          el.style.transform = "";
        }
      });
    });
  }

  // --- Lottie icons in workflow cards -----------------------------
  function initLottieIcons() {
    if (typeof window.lottie === "undefined") return;
    document.querySelectorAll("[data-lottie]").forEach((host) => {
      const path = host.dataset.lottie;
      if (!path) return;
      const mount = host.querySelector(".workflow-icon");
      if (!mount) return;
      const anim = window.lottie.loadAnimation({
        container: mount,
        path: path,
        renderer: "svg",
        loop: true,
        autoplay: true,
      });
      anim.setSpeed(0.6); // calm, ambient motion
      // Speed up briefly on hover for interactivity
      host.addEventListener("mouseenter", () => anim.setSpeed(1.4));
      host.addEventListener("mouseleave", () => anim.setSpeed(0.6));
    });
  }
})();
