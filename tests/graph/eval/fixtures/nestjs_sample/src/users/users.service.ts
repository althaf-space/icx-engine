import { Injectable } from "@nestjs/common";

export interface User {
  id: number;
  email: string;
  name: string;
}

@Injectable()
export class UsersService {
  private readonly users: User[] = [];

  create(email: string, name: string): User {
    const user: User = { id: this.users.length + 1, email, name };
    this.users.push(user);
    return user;
  }

  findOne(id: number): User | undefined {
    return this.users.find((user) => user.id === id);
  }
}
