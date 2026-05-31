export class UserService {
  async findAll(): Promise<any[]> {
    return [];
  }

  async findById(id: number): Promise<any | null> {
    return null;
  }

  async create(data: Record<string, unknown>): Promise<any> {
    return { id: 1, ...data };
  }
}
