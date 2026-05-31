import { router, publicProcedure } from "../trpc";
import { listUsers, getUser } from "../services/userService";

export const userRouter = router({
  list: publicProcedure.query(() => listUsers()),
  get: publicProcedure.input((id: number) => id).query(({ input }) => getUser(input)),
});
