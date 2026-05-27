import { UserService } from '../services/user.service';

export class UserResolver {
  constructor(private userService: UserService) {}

  async Query_users(_root: any, _args: any) {
    return this.userService.findAll();
  }

  async Query_user(_root: any, args: { id: number }) {
    return this.userService.findById(args.id);
  }
}
