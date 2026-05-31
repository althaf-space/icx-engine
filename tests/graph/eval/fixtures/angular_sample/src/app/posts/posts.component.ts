import { Component, OnInit } from "@angular/core";
import { PostsService, Post } from "./posts.service";

@Component({
  selector: "app-posts",
  template: `<article *ngFor="let p of posts"><h3>{{ p.title }}</h3></article>`,
})
export class PostsComponent implements OnInit {
  posts: Post[] = [];

  constructor(private readonly postsService: PostsService) {}

  async ngOnInit(): Promise<void> {
    this.posts = await this.postsService.list();
  }
}
