import { router, publicProcedure } from "../trpc";
import { listPosts, createPost } from "../services/postService";

export const postRouter = router({
  list: publicProcedure.query(() => listPosts()),
  create: publicProcedure
    .input((data: { title: string; authorId: number }) => data)
    .mutation(({ input }) => createPost(input.title, input.authorId)),
});
