"use client";

import { useRef } from "react";
import { ImagePlus, X, Info } from "lucide-react";

/**
 * Image/file drop-zone for the composer — attach mockups or screenshots as
 * reference (the "image uploads" Cursor capability).
 *
 * DASH-10 — HONEST about what it does today: there is no object-store upload
 * endpoint yet, so the blob bytes are NOT sent to the backend. We pass the
 * file NAMES as context hints (a crew member can read "match login-mock.png")
 * but the image content itself never leaves the browser. Rather than silently
 * dropping the bytes and pretending mockups were uploaded, the picker shows an
 * explicit "names only — bytes aren't uploaded yet" note so users don't expect
 * pixel-accurate matching that the platform can't deliver.
 *
 * When an upload endpoint lands, swap the dispatch path to upload each
 * `file` and pass the returned keys — the picker UI itself won't need to change.
 */

export interface Attachment {
  file: File;
  previewUrl: string;
}

export function AttachmentUpload({
  attachments,
  onChange,
  /**
   * When false, the picker is read-only and explains that image bytes can't be
   * uploaded yet (DASH-10). Defaults to enabled so existing callers keep their
   * filename-as-hint behaviour; pass `false` to fully gate it off.
   */
  enabled = true,
}: {
  attachments: Attachment[];
  onChange: (next: Attachment[]) => void;
  enabled?: boolean;
}) {
  const inputRef = useRef<HTMLInputElement | null>(null);

  function addFiles(files: FileList | null) {
    if (!files) return;
    const next = [...attachments];
    for (const file of Array.from(files)) {
      if (!file.type.startsWith("image/")) continue;
      next.push({ file, previewUrl: URL.createObjectURL(file) });
    }
    onChange(next);
  }

  function remove(idx: number) {
    const next = attachments.slice();
    const [removed] = next.splice(idx, 1);
    if (removed) URL.revokeObjectURL(removed.previewUrl);
    onChange(next);
  }

  if (!enabled) {
    return (
      <div
        className="flex items-center gap-2 rounded-md border border-dashed px-2.5 py-2 text-[11px]"
        style={{ borderColor: "var(--border-subtle)", color: "var(--ink-muted)" }}
        role="note"
      >
        <Info className="w-3.5 h-3.5 shrink-0" aria-hidden />
        Image upload isn’t available yet — describe mockups in the task instead.
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex flex-wrap items-center gap-2">
        {attachments.map((a, i) => (
          <div
            key={i}
            className="relative h-14 w-14 overflow-hidden rounded-md border"
            style={{ borderColor: "var(--border-subtle)" }}
            title={a.file.name}
          >
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img src={a.previewUrl} alt={a.file.name} className="h-full w-full object-cover" />
            <button
              type="button"
              onClick={() => remove(i)}
              className="absolute right-0 top-0 rounded-bl bg-black/60 p-0.5 text-white"
              aria-label={`Remove ${a.file.name}`}
              title="Remove"
            >
              <X size={11} />
            </button>
          </div>
        ))}
        <button
          type="button"
          onClick={() => inputRef.current?.click()}
          className="flex h-14 w-14 items-center justify-center rounded-md border border-dashed transition hover:opacity-80"
          style={{ borderColor: "var(--border-subtle)", color: "var(--ink-muted)" }}
          aria-label="Attach a reference image"
          title="Attach a reference image (filename is passed as a hint)"
        >
          <ImagePlus size={18} />
        </button>
        <input
          ref={inputRef}
          type="file"
          accept="image/*"
          multiple
          hidden
          onChange={(e) => {
            addFiles(e.target.files);
            // Allow re-selecting the same file after a remove.
            e.target.value = "";
          }}
        />
      </div>
      {attachments.length > 0 && (
        <span
          className="inline-flex items-center gap-1 text-[10.5px] leading-snug"
          style={{ color: "var(--ink-muted)" }}
        >
          <Info className="w-3 h-3 shrink-0" aria-hidden />
          Filenames are passed as hints — image bytes aren’t uploaded yet.
        </span>
      )}
    </div>
  );
}
