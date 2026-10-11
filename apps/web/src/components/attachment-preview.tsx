"use client";

import { useEffect, useRef } from "react";

import { CloseIcon } from "./icons";

export type PreviewAttachment = {
  sourceId: string | null;
  name: string;
  extension: string;
  sizeBytes: number | null;
  previewText: string | null;
  previewUrl: string | null;
  previewTruncated: boolean;
};

export type SelectedAttachment = PreviewAttachment & {
  file: File;
  indexed: boolean;
};

const FILE_NAME_LIMIT = 30;

export function truncateAttachmentName(name: string) {
  return name.length > FILE_NAME_LIMIT ? `${name.slice(0, FILE_NAME_LIMIT)}…` : name;
}

function fileTypeLabel(extension: string) {
  const label = extension.replace(".", "").toUpperCase();
  return label.length > 4 ? "FILE" : label || "FILE";
}

export function FileTypeIcon({ extension }: Readonly<{ extension: string }>) {
  return (
    <span aria-hidden="true" className="file-type-icon">
      <svg fill="none" viewBox="0 0 24 28">
        <path d="M4.5 1.5h10l5 5v20h-15z" stroke="currentColor" strokeLinejoin="round" />
        <path d="M14.5 1.5v5h5" stroke="currentColor" strokeLinejoin="round" />
      </svg>
      <small>{fileTypeLabel(extension)}</small>
    </span>
  );
}

export function AttachmentChip({ attachment, onOpen, onRemove }: Readonly<{
  attachment: SelectedAttachment;
  onOpen: () => void;
  onRemove: () => void;
}>) {
  return (
    <div className="attachment-chip-wrap">
      <button className="attachment-chip" onClick={onOpen} title={`Preview ${attachment.name}`} type="button">
        <FileTypeIcon extension={attachment.extension} />
        <span><strong>{truncateAttachmentName(attachment.name)}</strong><small>{attachment.sizeBytes === null ? "Indexed document" : `${(attachment.sizeBytes / 1024).toFixed(1)} KB`}</small></span>
      </button>
      <button aria-label={`Remove ${attachment.name}`} className="attachment-remove" onClick={onRemove} type="button"><CloseIcon /></button>
    </div>
  );
}

export function MessageAttachmentCard({ attachment, onOpen }: Readonly<{
  attachment: { source_id: string; name: string };
  onOpen: () => void;
}>) {
  const dot = attachment.name.lastIndexOf(".");
  const extension = dot >= 0 ? attachment.name.slice(dot).toLowerCase() : "";
  return (
    <button className="message-attachment" onClick={onOpen} title={`Preview ${attachment.name}`} type="button">
      <FileTypeIcon extension={extension} />
      <span><strong>{truncateAttachmentName(attachment.name)}</strong><small>Open document preview</small></span>
    </button>
  );
}

export function AttachmentPreview({ attachment, onClose }: Readonly<{
  attachment: PreviewAttachment;
  onClose: () => void;
}>) {
  const closeRef = useRef<HTMLButtonElement>(null);
  const dialogRef = useRef<HTMLDialogElement>(null);
  const onCloseRef = useRef(onClose);

  useEffect(() => {
    onCloseRef.current = onClose;
  }, [onClose]);

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    const activeDialog: HTMLDialogElement = dialog;
    const previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    dialog.showModal();
    closeRef.current?.focus();

    function onKeyDown(event: globalThis.KeyboardEvent) {
      if (event.key === "Escape") {
        event.preventDefault();
        onCloseRef.current();
        return;
      }
      if (event.key !== "Tab") return;
      const focusable = Array.from(
        activeDialog.querySelectorAll<HTMLElement>(
          'button:not([disabled]), a[href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), iframe, [tabindex]:not([tabindex="-1"])',
        ),
      );
      if (focusable.length === 0) {
        event.preventDefault();
        activeDialog.focus();
        return;
      }
      const first = focusable[0];
      const last = focusable.at(-1);
      if (!last) return;
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    }

    window.addEventListener("keydown", onKeyDown);
    return () => {
      window.removeEventListener("keydown", onKeyDown);
      dialog.close();
      previousFocus?.focus();
    };
  }, []);

  return (
    <dialog
      aria-describedby="attachment-preview-description"
      aria-labelledby="attachment-preview-title"
      className="attachment-modal glass-panel"
      onCancel={(event) => {
        event.preventDefault();
        onClose();
      }}
      onMouseDown={(event) => event.target === event.currentTarget && onClose()}
      ref={dialogRef}
    >
        <header>
          <div>
            <p id="attachment-preview-description">Attachment preview</p>
            <h2 id="attachment-preview-title">{attachment.name}</h2>
            <span>{fileTypeLabel(attachment.extension)}{attachment.sizeBytes === null ? "" : ` · ${(attachment.sizeBytes / 1024).toFixed(1)} KB`}</span>
          </div>
          <button aria-label="Close attachment preview" className="icon-button" onClick={onClose} ref={closeRef} type="button"><CloseIcon /></button>
        </header>
        <div className="attachment-preview-body">
          {attachment.previewUrl ? (
            <iframe src={attachment.previewUrl} title={`Preview of ${attachment.name}`} />
          ) : (
            <pre>{attachment.previewText ?? "A text preview is not available for this file."}</pre>
          )}
        </div>
        {attachment.previewTruncated && <p className="preview-note">Preview limited to the first 200,000 characters. The complete file is indexed.</p>}
    </dialog>
  );
}
