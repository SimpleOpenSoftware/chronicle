import { useEffect, useRef, ReactNode } from "react";

export default function ChatDialog({
  title,
  onClose,
  children,
}: {
  title: string;
  onClose: () => void;
  children: ReactNode;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const previous = document.activeElement as HTMLElement;
    ref.current?.showModal();
    return () => {
      ref.current?.close();
      previous?.focus();
    };
  }, []);
  return (
    <dialog
      ref={ref}
      aria-label={title}
      onCancel={(event) => {
        event.preventDefault();
        onClose();
      }}
      className="max-h-[92dvh] w-[min(48rem,96vw)] overflow-y-auto rounded-lg border border-[var(--tape-line)] bg-[var(--tape-paper)] p-0 text-[var(--tape-ink)] backdrop:bg-black/40"
    >
      <div className="sticky top-0 z-10 flex items-center justify-between gap-3 border-b border-[var(--tape-line)] bg-[var(--tape-paper)] p-4">
        <h2 className="font-semibold">{title}</h2>
        <button
          aria-label={`Close ${title}`}
          className="min-h-10 px-3"
          onClick={onClose}
        >
          Close
        </button>
      </div>
      <div className="p-4">{children}</div>
    </dialog>
  );
}
