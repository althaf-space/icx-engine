import { Component, OnInit } from "@angular/core";
import { UsersService, User } from "./users.service";

@Component({
  selector: "app-users",
  template: `<ul><li *ngFor="let u of users">{{ u.name }}</li></ul>`,
})
export class UsersComponent implements OnInit {
  users: User[] = [];

  constructor(private readonly usersService: UsersService) {}

  async ngOnInit(): Promise<void> {
    this.users = await this.usersService.list();
  }
}
