import { NgModule } from "@angular/core";
import { BrowserModule } from "@angular/platform-browser";
import { AppComponent } from "./app.component";
import { UsersComponent } from "./users/users.component";
import { PostsComponent } from "./posts/posts.component";
import { UsersService } from "./users/users.service";
import { PostsService } from "./posts/posts.service";

@NgModule({
  imports: [BrowserModule],
  declarations: [AppComponent, UsersComponent, PostsComponent],
  providers: [UsersService, PostsService],
  bootstrap: [AppComponent],
})
export class AppModule {}
