import { describe, it, expect } from 'vitest';
import { NAV_SECTIONS, CONTEXTUAL_ROUTES, isActiveSection } from '@/lib/nav';

describe('IA (REQ-001/002/003)', () => {
  it('top nav carries exactly the six public sections', () => {
    expect(NAV_SECTIONS.map((s) => s.label)).toEqual([
      'Competitions', 'Arena', 'Trials', 'Markets', 'Leaderboard', 'Agents',
    ]);
    expect(NAV_SECTIONS.map((s) => s.href)).toEqual([
      '/competitions', '/arena', '/trials', '/markets', '/leaderboard', '/agents',
    ]);
  });

  it('keeps Dashboard and contextual screens OUT of the public nav (not tabs)', () => {
    const navLabels = NAV_SECTIONS.map((s) => s.label);
    expect(navLabels).not.toContain('Dashboard');
    expect(navLabels).not.toContain('Proof Card');
    const ctxLabels = CONTEXTUAL_ROUTES.map((s) => s.label);
    expect(ctxLabels).toContain('Operator Dashboard');
    expect(ctxLabels).toContain('Design System');
  });

  it('marks a section active by path prefix', () => {
    expect(isActiveSection('/arena', '/arena')).toBe(true);
    expect(isActiveSection('/arena/wc-fra-bra', '/arena')).toBe(true);
    expect(isActiveSection('/markets', '/arena')).toBe(false);
    expect(isActiveSection('/', '/competitions')).toBe(false);
  });
});
