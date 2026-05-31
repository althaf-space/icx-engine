import { Injectable } from "@angular/core";
import { ApiService } from "../shared/api.service";

export interface Post {
  id: number;
  title: string;
  body: string;
}

@Injectable({ providedIn: "root" })
export class PostsService {
  constructor(private readonly api: ApiService) {}

  async list(): Promise<Post[]> {
    return this.api.fetchJson<Post[]>("/api/posts");
  }
}
