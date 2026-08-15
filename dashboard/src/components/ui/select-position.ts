/**
 * Where a dropdown menu goes, as arithmetic rather than as a rendered guess.
 *
 * Kept free of React so it can be exercised on its own — the anchoring is the
 * part that breaks, and it is invisible until something is on screen.
 */

export type TriggerRect = { left: number; top: number; bottom: number; width: number };
export type Viewport = { width: number; height: number };

export type MenuPlacement = {
  left: number;
  width: number;
  maxHeight: number;
  /** Exactly one of these is set — `bottom` anchors the menu's lower edge. */
  top?: number;
  bottom?: number;
};

const GAP = 6;
const MARGIN = 12;
const MIN_WIDTH = 240;
const MAX_HEIGHT = 320;
/** Below this, a menu shows too few rows to be worth dropping downward. */
const MIN_USABLE = 168;

export function placeMenu(rect: TriggerRect, viewport: Viewport): MenuPlacement {
  const below = viewport.height - rect.bottom - GAP - MARGIN;
  const above = rect.top - GAP - MARGIN;
  const dropUp = below < MIN_USABLE && above > below;

  const width = Math.min(Math.max(rect.width, MIN_WIDTH), viewport.width - 2 * MARGIN);
  const left = Math.min(Math.max(rect.left, MARGIN), viewport.width - width - MARGIN);
  const maxHeight = Math.max(96, Math.min(MAX_HEIGHT, dropUp ? above : below));

  return dropUp
    ? { left, width, maxHeight, bottom: viewport.height - rect.top + GAP }
    : { left, width, maxHeight, top: rect.bottom + GAP };
}
