import {
  useQuery,
  useInfiniteQuery,
  useMutation,
  useQueryClient,
} from "@tanstack/react-query";
import { chatApi } from "../services/api";

export function useChatSessions(memorySpaceId?: string) {
  return useQuery({
    queryKey: ["chat", "sessions", memorySpaceId],
    queryFn: ({ signal }) => chatApi.getSessions(50, memorySpaceId, signal).then((r) => r.data),
    retry: false,
  });
}

export function useChatMessages(sessionId: string | null) {
  const query = useInfiniteQuery({
    queryKey: ["chat", "messages", sessionId],
    initialPageParam: 0,
    queryFn: ({ pageParam }) =>
      chatApi.getMessages(sessionId!, 100, pageParam).then((r) => r.data),
    getNextPageParam: (last, pages) =>
      last.length === 100 ? pages.length * 100 : undefined,
    enabled: !!sessionId,
  });
  return {
    ...query,
    data: query.data ? [...query.data.pages].reverse().flat() : undefined,
  };
}

export function useCreateChatSession(memorySpaceId?: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (title?: string) =>
      chatApi
        .createSession(title, undefined, memorySpaceId)
        .then((r) => r.data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["chat", "sessions"] });
    },
  });
}

export function useDeleteChatSession() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (sessionId: string) => chatApi.deleteSession(sessionId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["chat", "sessions"] });
    },
  });
}
