import { Injectable } from "@angular/core";
import { ApiService } from "../shared/api.service";

export interface User {
  id: number;
  email: string;
  name: string;
}

@Injectable({ providedIn: "root" })
export class UsersService {
  constructor(private readonly api: ApiService) {}

  async list(): Promise<User[]> {
    return this.api.fetchJson<User[]>("/api/users");
  }
}
