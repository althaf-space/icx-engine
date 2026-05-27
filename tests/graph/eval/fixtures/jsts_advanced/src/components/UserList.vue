<template>
  <div>
    <div v-for="user in sortedUsers" :key="user.id">
      <PostCard :user="user" />
    </div>
  </div>
</template>

<script>
import PostCard from './PostCard.vue';

export default {
  name: 'UserList',
  components: { PostCard },
  data() {
    return {
      users: [],
      searchQuery: '',
    };
  },
  computed: {
    sortedUsers() {
      return this.users.filter(u => u.name.includes(this.searchQuery));
    },
  },
  methods: {
    fetchUsers() {
      this.users = [];
    },
    deleteUser(id) {
      this.users = this.users.filter(u => u.id !== id);
    },
  },
  mounted() {
    this.fetchUsers();
  },
};
</script>
