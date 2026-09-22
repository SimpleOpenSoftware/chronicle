import { useMemo, useEffect } from "react";
import { useQuery } from "@tanstack/react-query";
import { manualMemoriesApi } from "../services/api";
const MANUAL_MEMORY_NOTE = /(?:^|\/)Manual Memories\/([0-9a-f-]{36})\.md$/i;

// The endpoint is authenticated, so the image is fetched as a blob through the API
// client and shown from an object URL. A bare <img src="/api/..."> sends no
// Authorization header and 401s — same reason EpisodeThumbnail does it this way.
function CitedImage({ memoryId }: { memoryId: string }) {
  const memory = useQuery({
    queryKey: ["manual-memory", memoryId],
    queryFn: async () => (await manualMemoriesApi.get(memoryId)).data,
  });
  const attachment = memory.data?.attachments[0];
  const thumbnail = useQuery({
    queryKey: ["chat-cited-manual-memory", memoryId, attachment?.attachment_id],
    queryFn: async () =>
      (
        await manualMemoriesApi.getThumbnail(
          memoryId,
          attachment!.attachment_id,
        )
      ).data,
    enabled: Boolean(attachment),
    staleTime: Infinity,
    retry: false,
  });
  const url = useMemo(
    () => (thumbnail.data ? URL.createObjectURL(thumbnail.data) : null),
    [thumbnail.data],
  );
  useEffect(
    () => () => {
      if (url) URL.revokeObjectURL(url);
    },
    [url],
  );
  // A pruned or unreadable image simply is not shown; no broken-image glyph.
  if (!url) return null;
  return (
    <a
      href={url}
      target="_blank"
      rel="noreferrer"
      title="Open the saved image"
      className="block overflow-hidden rounded border border-gray-200 hover:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-500 dark:border-gray-700"
    >
      <img
        src={url}
        alt="Image from a cited manual memory"
        className="h-24 w-auto max-w-[12rem] object-cover"
      />
    </a>
  );
}

export default function CitedImages({
  memoriesUsed,
}: {
  memoriesUsed: string[];
}) {
  const memoryIds = Array.from(
    new Set(
      (memoriesUsed || [])
        .map((path) => path.match(MANUAL_MEMORY_NOTE)?.[1]?.toLowerCase())
        .filter((memoryId): memoryId is string => Boolean(memoryId)),
    ),
  );
  if (memoryIds.length === 0) return null;
  return (
    <div className="mt-3 flex flex-wrap gap-2">
      {memoryIds.map((memoryId) => (
        <CitedImage key={memoryId} memoryId={memoryId} />
      ))}
    </div>
  );
}
