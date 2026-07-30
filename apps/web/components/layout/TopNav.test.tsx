import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { TopNav } from '@/components/layout/TopNav';

vi.mock('next/navigation', () => ({ usePathname: () => '/markets' }));

describe('TopNav (REQ-001)', () => {
  it('renders the six public sections and nothing else as tabs', () => {
    render(<TopNav />);
    for (const label of ['Competitions', 'Arena', 'Trials', 'Markets', 'Leaderboard', 'Agents']) {
      expect(screen.getByRole('link', { name: label })).toBeInTheDocument();
    }
    expect(screen.queryByRole('link', { name: 'My Agents' })).toBeNull();
  });

  it('marks the active section with aria-current', () => {
    render(<TopNav />);
    expect(screen.getByRole('link', { name: 'Markets' })).toHaveAttribute('aria-current', 'page');
    expect(screen.getByRole('link', { name: 'Arena' })).not.toHaveAttribute('aria-current');
  });
});
