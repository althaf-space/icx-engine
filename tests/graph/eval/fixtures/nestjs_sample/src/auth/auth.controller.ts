import { Body, Controller, Post } from "@nestjs/common";
import { AuthService } from "./auth.service";

@Controller("auth")
export class AuthController {
  constructor(private readonly authService: AuthService) {}

  @Post("token")
  issue(@Body() payload: { userId: number }): { token: string } {
    return { token: this.authService.issueToken(payload.userId) };
  }
}
