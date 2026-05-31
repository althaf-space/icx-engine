import { createSlice, PayloadAction } from '@reduxjs/toolkit';

const postsSlice = createSlice({
  name: 'posts',
  initialState: { items: [] as any[] },
  reducers: {
    setPosts(state, action: PayloadAction<any[]>) {
      state.items = action.payload;
    },
  },
});

export const { setPosts } = postsSlice.actions;
export default postsSlice.reducer;
