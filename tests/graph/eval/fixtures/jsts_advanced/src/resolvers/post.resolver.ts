import { PostService } from '../services/post.service';

export class PostResolver {
  constructor(private postService: PostService) {}

  async Query_posts(_root: any) {
    return this.postService.findAll();
  }

  async User_posts(user: { id: number }) {
    return this.postService.findByAuthor(user.id);
  }
}
