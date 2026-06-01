"use client";

import { useRef } from "react";
import { ImagePlus, X } from "lucide-react";

/**
 * Image/file drop-zone for the composer — attach mockups or screenshots as
 * reference (the "image uploads" Cursor capability). Holds local File objects
 * + preview URLs; the page uploads them to the object store on dispatch and
 * passes the resulting keys as `attachments`.
 */

export interface Attachment {
  file: File;
  previewUrl: string;
}

export function AttachmentUpload({
  attachments,
  onChange,
}: {
  attachments: Attachment[];
  onChange: (next: Attachment[]) => void;
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

  return (
    <div className="flex flex-wrap items-center gap-2">
      {attachments.map((a, i) => (
        <div
          key={i}
          className="relative h-14 w-14 overflow-hidden rounded-md border"
          style={{ borderColor: "var(--border-subtle)" }}
        >
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img src={a.previewUrl} alt={a.file.name} className="h-full w-full object-cover" />
          <button
            type="button"
            onClick={() => remove(i)}
            className="absolute right-0 top-0 rounded-bl bg-black/60 p-0.5 text-white"
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
        style={{ borderColor: "var(--border-subtle)", color: "var(--text-muted)" }}
        title="Attach an image"
      >
        <ImagePlus size={18} />
      </button>
      <input
        ref={inputRef}
        type="file"
        accept="image/*"
        multiple
        hidden
        onChange={(e) => addFiles(e.target.files)}
      />
    </div>
  );
}
