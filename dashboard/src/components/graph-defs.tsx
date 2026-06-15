/**
 * Reusable SVG <defs> for every graph view (lanes DAG, pipeline mesh, boardroom
 * mesh). One arrowhead whose color FOLLOWS the referencing line's stroke
 * (`context-stroke`), so a single marker serves flow / challenge / spoke / done
 * edges and every arrowhead automatically matches its line — including the
 * theme-aware `--graph-*` CSS variables. That keeps arrows prominent and
 * identical across all graphs in both dark and light mode.
 *
 * `idPrefix` scopes the marker/filter ids per SVG so multiple graphs on one
 * page never collide. Reference them as `url(#<idPrefix>-arrow)` /
 * `filter="url(#<idPrefix>-shadow)"`.
 */
export function GraphDefs({ idPrefix }: { idPrefix: string }) {
  return (
    <defs>
      <marker
        id={`${idPrefix}-arrow`}
        markerWidth="11"
        markerHeight="11"
        refX="8.5"
        refY="3.6"
        orient="auto"
        markerUnits="userSpaceOnUse"
      >
        {/* context-stroke → the arrowhead paints with the line's stroke color */}
        <path d="M0,0 L8.5,3.6 L0,7.2 Z" fill="context-stroke" />
      </marker>
      <filter id={`${idPrefix}-shadow`} x="-50%" y="-50%" width="200%" height="200%">
        <feDropShadow dx="0" dy="1.5" stdDeviation="2.5" floodColor="#000" floodOpacity="0.4" />
      </filter>
    </defs>
  );
}

// Semantic edge colors — reference the theme-aware CSS vars so every graph is
// consistent and prominent in both modes. Used as `stroke` (the arrowhead
// follows via context-stroke).
export const EDGE = {
  flow: "var(--graph-edge)",
  spoke: "var(--graph-spoke)",
  challenge: "var(--graph-challenge)",
  done: "var(--graph-done)",
} as const;
