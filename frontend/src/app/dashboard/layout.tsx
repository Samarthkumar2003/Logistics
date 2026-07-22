import type { ReactNode } from 'react';

// Applied before the dashboard paints so a stored light theme never flashes
// dark first. Dark is the default, so no-op for anyone who never toggled.
const THEME_INIT = `(function(){try{var t=localStorage.getItem('dashboard-theme');document.documentElement.dataset.theme=t==='light'?'light':'dark';}catch(e){document.documentElement.dataset.theme='dark';}})();`;

export default function DashboardLayout({ children }: { children: ReactNode }) {
  return (
    <>
      <script dangerouslySetInnerHTML={{ __html: THEME_INIT }} />
      {children}
    </>
  );
}
