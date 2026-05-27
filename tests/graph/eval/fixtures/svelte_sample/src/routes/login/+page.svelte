<script lang="ts">
  import { currentUser } from "$lib/stores/auth";
</script>

<h1>Login</h1>
{#if !$currentUser}
  <form method="POST">
    <input name="userId" type="number" />
    <button type="submit">Login</button>
  </form>
{/if}
