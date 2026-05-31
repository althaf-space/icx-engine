<script lang="ts">
  import { currentUser } from "../stores/auth";
</script>

<nav>
  <a href="/">Home</a>
  {#if $currentUser}
    <span>{$currentUser.name}</span>
  {/if}
</nav>
