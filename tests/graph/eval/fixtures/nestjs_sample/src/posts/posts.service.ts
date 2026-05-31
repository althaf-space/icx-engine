import { Injectable } from "@nestjs/common";
import { UsersService } from "../users/users.service";

export interface Post {
  id: number;
  authorId: number;
  title: string;
  body: string;
}

@Injectable()
export class PostsService {
  private readonly posts: Post[] = [];

  constructor(private readonly usersService: UsersService) {}

  publish(authorId: number, title: string, body: string): Post {
    const author = this.usersService.findOne(authorId);
    if (!author) throw new Error("author not found");
    const post: Post = { id: this.posts.length + 1, authorId, title, body };
    this.posts.push(post);
    return post;
  }

  listForAuthor(authorId: number): Post[] {
    return this.posts.filter((post) => post.authorId === authorId);
  }
}
