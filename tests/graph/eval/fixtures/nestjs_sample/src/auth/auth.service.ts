import { Injectable } from "@nestjs/common";
import { UsersService, User } from "../users/users.service";

@Injectable()
export class AuthService {
  constructor(private readonly usersService: UsersService) {}

  issueToken(userId: number): string {
    const user: User | undefined = this.usersService.findOne(userId);
    if (!user) throw new Error("no user");
    return `token-${user.id}`;
  }
}
