// Information architecture single source (REQ-001/002/003).
export const NAV_SECTIONS = [
  { label: 'Competitions', href: '/competitions' },
  { label: 'Arena', href: '/arena' },
  { label: 'Trials', href: '/trials' },
  { label: 'Markets', href: '/markets' },
  { label: 'Leaderboard', href: '/leaderboard' },
  { label: 'Agents', href: '/agents' },
] as const;

// Reached by deep-link / the wallet dropdown — NEVER top-level tabs (REQ-002/003).
export const CONTEXTUAL_ROUTES = [
  { label: 'Operator Dashboard', href: '/dashboard' },
  { label: 'Agent Studio', href: '/studio' },
  { label: 'Create Competition', href: '/competitions/create' },
  { label: 'Proof Card', href: '/proof/sample' },
  { label: 'Head-to-Head Duel', href: '/duel' },
  { label: 'Clone Preview', href: '/clone' },
  { label: 'Prize Vault', href: '/vault' },
  { label: 'Design System', href: '/design-system' },
] as const;

export function isActiveSection(pathname: string, href: string): boolean {
  if (href === '/') return pathname === '/';
  return pathname === href || pathname.startsWith(`${href}/`);
}
