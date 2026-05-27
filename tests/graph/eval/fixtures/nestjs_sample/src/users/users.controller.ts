import { Body, Controller, Get, Param, Post } from "@nestjs/common";
import { UsersService, User } from "./users.service";

@Controller("users")
export class UsersController {
  constructor(private readonly usersService: UsersService) {}

  @Post()
  register(@Body() payload: { email: string; name: string }): User {
    return this.usersService.create(payload.email, payload.name);
  }

  @Get(":id")
  read(@Param("id") id: string): User | undefined {
    return this.usersService.findOne(Number(id));
  }
}
