import { Component } from "@angular/core";

@Component({
  selector: "app-root",
  template: `
    <app-users></app-users>
    <app-posts></app-posts>
  `,
})
export class AppComponent {
  title = "Blog";
}
