import { Body, Controller, Get, Param, Post as HttpPost } from "@nestjs/common";
import { PostsService, Post } from "./posts.service";

@Controller("posts")
export class PostsController {
  constructor(private readonly postsService: PostsService) {}

  @Get("by-author/:authorId")
  list(@Param("authorId") authorId: string): Post[] {
    return this.postsService.listForAuthor(Number(authorId));
  }

  @HttpPost("by-author/:authorId")
  publish(
    @Param("authorId") authorId: string,
    @Body() payload: { title: string; body: string },
  ): Post {
    return this.postsService.publish(Number(authorId), payload.title, payload.body);
  }
}
