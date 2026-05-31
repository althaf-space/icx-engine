import { Injectable } from "@angular/core";

@Injectable({ providedIn: "root" })
export class ApiService {
  async fetchJson<T>(url: string): Promise<T> {
    return {} as T;
  }
}
