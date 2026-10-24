/* Light/dark theme. The choice (auto, light or dark) is kept per browser and
   auto follows the system. An inline script in each page's <head> applies it
   before first paint; this module handles the button and later changes. */

const KEY = 'theme';
const CHOICES = {
    auto:  { next: 'light', icon: 'fa-circle-half-stroke', title: 'Theme: auto, following your system' },
    light: { next: 'dark',  icon: 'fa-sun',                title: 'Theme: light' },
    dark:  { next: 'auto',  icon: 'fa-moon',               title: 'Theme: dark' },
};

const systemLight = window.matchMedia('(prefers-color-scheme: light)');

function readChoice() {
    try {
        const saved = localStorage.getItem(KEY);
        return CHOICES[saved] ? saved : 'auto';
    } catch {
        return 'auto';
    }
}

function apply(choice) {
    const theme = choice === 'auto' ? (systemLight.matches ? 'light' : 'dark') : choice;
    const root = document.documentElement;
    if (root.dataset.theme !== theme) {
        root.dataset.theme = theme;
        // Canvas charts cache their colours; this tells them to redraw.
        window.dispatchEvent(new Event('themechange'));
    }
    document.querySelector('meta[name="theme-color"]')
        ?.setAttribute('content', theme === 'light' ? '#f6f7f9' : '#0a0a0d');

    const btn = document.getElementById('theme-btn');
    if (btn) {
        btn.firstElementChild.className = `fa-solid ${CHOICES[choice].icon}`;
        btn.title = `${CHOICES[choice].title}. Click to change.`;
        btn.setAttribute('aria-label', btn.title);
    }
}

systemLight.addEventListener('change', () => apply(readChoice()));

export function mountThemeToggle() {
    const btn = document.getElementById('theme-btn');
    if (!btn) return;
    btn.addEventListener('click', () => {
        const next = CHOICES[readChoice()].next;
        try {
            localStorage.setItem(KEY, next);
        } catch {
            // Storage blocked: the change still applies to this page.
        }
        apply(next);
    });
    apply(readChoice());
}
