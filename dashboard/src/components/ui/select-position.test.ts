import { strict as assert } from "node:assert";
import { test } from "node:test";
import { placeMenu, type MenuPlacement, type TriggerRect } from "./select-position.ts";

const VIEWPORT = { width: 1440, height: 900 };

const trigger = (top: number, width = 400, left = 300): TriggerRect => ({
  left,
  top,
  bottom: top + 40,
  width,
});

const topEdge = (p: MenuPlacement, viewportHeight = VIEWPORT.height) =>
  p.top ?? viewportHeight - (p.bottom ?? 0) - p.maxHeight;

test("drops below the trigger when there is room", () => {
  const p = placeMenu(trigger(200), VIEWPORT);
  assert.equal(p.top, 246);
  assert.equal(p.bottom, undefined);
});

test("grows upward from just above the trigger when the space below is tight", () => {
  const rect = trigger(760);
  const p = placeMenu(rect, VIEWPORT);
  assert.equal(p.top, undefined);
  // The menu's lower edge sits a gap above the field — not pinned to the top
  // of the screen with the field stranded far below it.
  assert.equal(VIEWPORT.height - (p.bottom ?? 0), rect.top - 6);
});

test("a flipped menu never covers the trigger and stays on screen", () => {
  const rect = trigger(760);
  const p = placeMenu(rect, VIEWPORT);
  const bottomEdge = VIEWPORT.height - (p.bottom ?? 0);
  assert.ok(bottomEdge <= rect.top, "menu overlaps the field it belongs to");
  assert.ok(topEdge(p) >= 12, "menu runs off the top of the screen");
});

test("keeps a dropped menu inside the viewport", () => {
  const p = placeMenu(trigger(200), VIEWPORT);
  assert.ok((p.top ?? 0) + p.maxHeight <= VIEWPORT.height - 12);
});

test("prefers dropping down while the space below still shows several rows", () => {
  // The Tools field of the new-sandbox modal: room below, so no flip.
  const p = placeMenu({ left: 178, top: 502, bottom: 542, width: 676 }, { width: 1130, height: 776 });
  assert.equal(p.top, 548);
});

test("widens a narrow trigger so descriptions stay readable", () => {
  const p = placeMenu(trigger(200, 120), VIEWPORT);
  assert.equal(p.width, 240);
});

test("pulls a menu back from the right edge", () => {
  const p = placeMenu(trigger(200, 400, 1300), VIEWPORT);
  assert.equal(p.left, 1440 - 400 - 12);
});

test("never collapses to an unusable height", () => {
  const p = placeMenu({ left: 10, top: 300, bottom: 340, width: 200 }, { width: 400, height: 380 });
  assert.ok(p.maxHeight >= 96);
});
